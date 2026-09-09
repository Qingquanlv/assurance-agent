from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskHandler
from tests.product.test_change_local_output_routing import execute_task

from assurance_improvement.contracts.agent import RetroAnalysisResultV3
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_improvement.contracts.improvements import ImprovementKind, ImprovementState
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    WorkflowEvidenceSlice,
)
from assurance_improvement.operations.agent import (
    RetroEvalPrepareHandler,
    RetroFinalizeHandler,
    RetroIssueFinalizeHandler,
    RetroIssuePrepareHandler,
    RetroPrepareHandler,
    RetroWorkflowPrepareHandler,
)
from assurance_improvement.operations.retro import (
    AssembleRetroContextHandler,
    ReconcileInput,
    reconcile_improvements,
    assemble_context,
    AssembleRetroInput,
    RetroCollectHandler,
)
from assurance_improvement.resource_loader import resource_bytes
from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    HEX_A,
    RETRO_ID,
    as_object,
    candidate_payload,
    fake_agent_result,
    improvement_projection,
    json_value,
    retro_result,
    issue_signal,
)

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_improvement" / "resources"
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


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def _retro_input(domain=None):
    if domain is not None:
        return {"change_id": "CH-DEMO-001", "evidence_slice": _empty_slice(domain)}
    context = assemble_context(AssembleRetroInput.model_validate(_assemble_payload()))
    payload = context.model_dump(mode="json")
    payload["signals"]["issue"] = [issue_signal()]
    payload["signal_count"] = 1
    return {"change_id": "CH-DEMO-001", "context": payload}


def _finalize_payload(tmp_path, *, source_id="PROB-1", domain=None, result_domain=None):
    document = dict(retro_result(source_id=source_id, domain=result_domain or domain))
    if domain is None:
        document["signals"] = []
    else:
        document["candidates"] = []
    payload = {**_retro_input(domain), "agent_result": fake_agent_result(document)["agent_result"]}
    from tests.product.test_change_local_output_routing import dual_roots

    _, stage = dual_roots(tmp_path)
    suffix = f"retro-{domain}-analysis" if domain else "retro"
    path = stage / f"qa/changes/CH-DEMO-001/retro/{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return payload


@pytest.mark.asyncio
async def test_retro_finalize_rejects_signal_outside_manifest(tmp_path: Path) -> None:
    outcome = await execute_task(
        RetroFinalizeHandler(),
        _finalize_payload(tmp_path, source_id="unknown"),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_retro_finalize_accepts_authenticated_source(tmp_path: Path) -> None:
    outcome = await execute_task(
        RetroFinalizeHandler(),
        _finalize_payload(tmp_path),
        tmp_path,
    )
    assert outcome.status == "succeeded", outcome.failure.message if outcome.failure else None
    document = RetroAnalysisResultV3.model_validate(outcome.output)
    assert document.retro_id == RETRO_ID
    assert document.candidates[0].source_refs.problem_ids == ("PROB-1",)


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    first = await execute_task(RetroPrepareHandler(), _retro_input(), tmp_path, binding_data=BINDING)
    second = await execute_task(RetroPrepareHandler(), _retro_input(), tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    request = AgentRunRequest.model_validate(first.output)
    assert request.canonical_bytes() == AgentRunRequest.model_validate(second.output).canonical_bytes()
    skill, persona, business = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned retro skill" in (skill.text_content or "")
    assert "Improvement reviewer persona" in (persona.text_content or "")
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **cast(dict[str, object], BINDING["execution"]),
            "provider_model": "primary,fallback",
        },
    }
    outcome = await execute_task(
        RetroPrepareHandler(),
        _retro_input(),
        tmp_path,
        binding_data=cast(JSONValue, binding),
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "marker"),
    (
        (RetroPrepareHandler(), "Capability-owned retro skill"),
        (RetroEvalPrepareHandler(), "Capability-owned retro-eval-analysis skill"),
        (RetroIssuePrepareHandler(), "Capability-owned retro-issue-analysis skill"),
        (RetroWorkflowPrepareHandler(), "Capability-owned retro-workflow-analysis skill"),
    ),
)
async def test_each_prepare_locks_skill_persona_and_execution(
    handler: TaskHandler, marker: str, tmp_path: Path
) -> None:
    domain = {
        RetroEvalPrepareHandler: "eval",
        RetroIssuePrepareHandler: "issue",
        RetroWorkflowPrepareHandler: "workflow",
    }.get(type(handler))
    outcome = await execute_task(handler, _retro_input(domain), tmp_path, binding_data=BINDING)
    assert outcome.status == "succeeded"
    request = AgentRunRequest.model_validate(outcome.output)
    assert marker in (request.instructions[0].text_content or "")
    assert request.execution.provider_model == "test-model"


def test_resources_have_no_forbidden_provider_tokens() -> None:
    for path in _resource_files():
        text = path.read_text(encoding="utf-8")
        match = _TOKEN.search(text)
        assert match is None, f"{path}: found {match.group(0)!r}"
        lowered = text.lower()
        for token in _FORBIDDEN:
            assert token not in lowered


def test_result_contract_bytes_equal_typed_models() -> None:
    assert resource_bytes("result-contracts/retro-analysis.v3.schema.json") == canonical_json_bytes(
        RetroAnalysisResultV3.model_json_schema()
    )


@pytest.mark.asyncio
async def test_domain_finalize_rejects_mismatched_domain(tmp_path: Path) -> None:
    outcome = await execute_task(
        RetroIssueFinalizeHandler(),
        _finalize_payload(tmp_path, domain="issue", result_domain="eval"),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_domain_finalize_accepts_matching_domain(tmp_path: Path) -> None:
    outcome = await execute_task(
        RetroIssueFinalizeHandler(),
        _finalize_payload(tmp_path, domain="issue"),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    document = RetroAnalysisResultV3.model_validate(outcome.output)
    assert document.domain == "issue"


@pytest.mark.asyncio
async def test_assemble_rejects_incomplete_optional_domain() -> None:
    payload = {
        "generated_at": "2026-08-22T00:00:00Z",
        "window": {
            "selection": {"mode": "last", "requested_last": 1},
            "change_ids": ["CH-DEMO-001"],
        },
        "issue_slice": _empty_slice("issue"),
        "workflow_slice": _empty_slice("workflow"),
        "eval_slice": _empty_slice("eval"),
        "discovery_slice": _empty_slice("discovery"),
        "issue_signals": _ok_signals("issue"),
        "workflow_signals": _ok_signals("workflow"),
        "eval_signals": _ok_signals("eval"),
        "issue_slice_sha256": HEX_A,
        "workflow_slice_sha256": HEX_A,
        "eval_slice_sha256": HEX_A,
    }
    outcome = await execute_task(AssembleRetroContextHandler(), payload)
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


def _empty_slice(domain: str) -> dict[str, object]:
    window = {"selection": {"mode": "last", "requested_last": 1}, "change_ids": ["CH-DEMO-001"]}
    sources = (
        [
            {
                "kind": "project_problem_ledger",
                "change_id": None,
                "head_event_id": "evt-1",
                "sha256": "abc",
                "evidence_ids": ["PROB-1", "OCC-1"],
            }
        ]
        if domain == "issue"
        else []
    )
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": domain,
        "window": window,
        "sources": sources,
        "integrity": {"status": "complete", "reasons": []},
        "deterministic_signals": [],
        "entries": [],
    }


def _ok_signals(domain: str, digest: str = HEX_A) -> dict[str, object]:
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": domain,
        "analysis_status": "ok",
        "failure_reason": None,
        "analyzer": f"aa-retro-{domain}-analysis",
        "signals": [],
        "slice_sha256": digest,
    }


def _slice_model(domain: str) -> IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice:
    raw = _empty_slice(domain)
    if domain == "issue":
        return IssueEvidenceSlice.model_validate(raw)
    if domain == "workflow":
        return WorkflowEvidenceSlice.model_validate(raw)
    return EvalEvidenceSlice.model_validate(raw)


def _assemble_payload(**overrides: object) -> dict[str, object]:
    issue = _slice_model("issue")
    workflow = _slice_model("workflow")
    evaluation = _slice_model("eval")
    payload: dict[str, object] = {
        "generated_at": "2026-08-22T00:00:00Z",
        "window": {
            "selection": {"mode": "last", "requested_last": 1},
            "change_ids": ["CH-DEMO-001"],
        },
        "issue_slice": issue.model_dump(mode="json"),
        "workflow_slice": workflow.model_dump(mode="json"),
        "eval_slice": evaluation.model_dump(mode="json"),
        "issue_signals": _ok_signals("issue", artifact_digest(issue)),
        "workflow_signals": _ok_signals("workflow", artifact_digest(workflow)),
        "eval_signals": _ok_signals("eval", artifact_digest(evaluation)),
        "issue_slice_sha256": artifact_digest(issue),
        "workflow_slice_sha256": artifact_digest(workflow),
        "eval_slice_sha256": artifact_digest(evaluation),
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_assemble_rejects_unauthenticated_slice_digest() -> None:
    payload = _assemble_payload(issue_slice_sha256=HEX_A)
    outcome = await execute_task(AssembleRetroContextHandler(), json_value(payload))
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_assemble_rejects_signal_domain_mismatch() -> None:
    payload = _assemble_payload()
    issue_signals = as_object(payload["issue_signals"])
    issue_signals["domain"] = "workflow"
    payload["issue_signals"] = issue_signals
    outcome = await execute_task(AssembleRetroContextHandler(), json_value(payload))
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_assemble_rejects_mixed_retro_ids() -> None:
    payload = _assemble_payload()
    workflow = as_object(payload["workflow_slice"])
    workflow["retro_id"] = "RET-OTHER"
    signals = as_object(payload["workflow_signals"])
    signals["retro_id"] = "RET-OTHER"
    payload["workflow_slice"] = workflow
    payload["workflow_signals"] = signals
    payload["workflow_slice_sha256"] = artifact_digest(WorkflowEvidenceSlice.model_validate(workflow))
    signals["slice_sha256"] = payload["workflow_slice_sha256"]
    payload["workflow_signals"] = signals
    outcome = await execute_task(AssembleRetroContextHandler(), json_value(payload))
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_collect_rejects_mismatched_slice_retro_id() -> None:
    window = {"selection": {"mode": "last", "requested_last": 1}, "change_ids": ["CH-DEMO-001"]}
    workflow = _empty_slice("workflow")
    workflow["retro_id"] = "RET-OTHER"
    outcome = await execute_task(
        RetroCollectHandler(),
        json_value(
            {
                "retro_id": RETRO_ID,
                "window": window,
                "issue_slice": _empty_slice("issue"),
                "workflow_slice": workflow,
                "eval_slice": _empty_slice("eval"),
            }
        ),
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_reconcile_persists_knowledge_delta_and_supersedes() -> None:
    assembled = await execute_task(AssembleRetroContextHandler(), json_value(_assemble_payload()))
    assert assembled.status == "succeeded"
    context = as_object(assembled.output)
    predecessor_id = "IMP-OLD"
    current = {
        "schema_version": "1",
        "last_seq": 1,
        "improvements": {
            predecessor_id: improvement_projection(
                state="proposed",
                delivery="change_draft",
                improvement_id=predecessor_id,
            )
        },
        "by_fingerprint": {},
    }
    delta = {
        "schema_version": "1",
        "mode": "delta",
        "entities": {"dept": {"required_fields": ["name"]}},
    }
    candidate = candidate_payload(
        kind="domain_knowledge",
        delivery="knowledge_delta",
        target=".aa/data-knowledge.yaml",
        knowledge_delta=delta,
        supersedes=predecessor_id,
    )
    payload = as_object(
        reconcile_improvements(
            ReconcileInput.model_validate(
                {
                    "context": context,
                    "candidates": [candidate],
                    "current": current,
                    "ts": "2026-08-22T00:00:00Z",
                }
            )
        )
    )
    improvements = as_object(payload["improvements"])
    new_id = payload["improvement_ids"][0]
    assert as_object(improvements[new_id])["knowledge_delta"]["mode"] == "delta"
    assert as_object(improvements[new_id])["knowledge_delta"]["entities"]["dept"]["required_fields"] == [
        "name"
    ]
    assert as_object(improvements[predecessor_id])["state"] == ImprovementState.SUPERSEDED.value
    assert any(event["type"] == "improvement_superseded" for event in payload["events"])
    assert as_object(improvements[new_id])["kind"] == ImprovementKind.DOMAIN_KNOWLEDGE.value


@pytest.mark.asyncio
async def test_failed_retro_validation_leaves_canonical_outputs_unchanged(tmp_path: Path) -> None:
    from tests.product.test_change_local_output_routing import dual_roots

    project, write_root = dual_roots(tmp_path)
    canonical = project / "qa/changes/CH-DEMO-001/retro/retro.json"
    canonical.parent.mkdir(parents=True)
    original = b'{"schema_version":"3"}\n'
    canonical.write_bytes(original)
    outcome = await execute_task(
        RetroFinalizeHandler(),
        {"agent_result": {}},
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert canonical.read_bytes() == original
