from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskHandler
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.phase4.conformance import execute_task

from assurance_quality.contracts.agent import (
    FactBaselineResultV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    ReportResultV1,
)
from assurance_quality.operations.agent_skills import (
    FactBaselineFinalizeHandler,
    FactBaselinePrepareHandler,
    InspectFinalizeHandler,
    InspectPrepareHandler,
    IssueAnalysisFinalizeHandler,
    IssueAnalysisPrepareHandler,
    IssueTriageFinalizeHandler,
    IssueTriagePrepareHandler,
    ReportFinalizeHandler,
    ReportPrepareHandler,
)
from assurance_quality.resource_loader import resource_bytes
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    EVIDENCE_REF,
    HEX_A,
    HEX_B,
    LEAF,
    as_object,
)

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_quality" / "resources"
_SHA = HEX_A
_OWNED = "OBS-owned"
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


def issue_candidate(*, evidence_ids: list[str]) -> JSONValue:
    return cast(
        JSONValue,
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "evidence_bundle_digest": EVIDENCE_REF,
            "status": "completed",
            "candidate_count": 1,
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": evidence_ids,
                    "proposed": {
                        "title": "boom",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "x",
                    },
                    "affected_surface": {"kind": "module", "value": "Menus Service"},
                    "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                    "possible_problem_ids": [],
                    "confidence": 0.8,
                    "recommended_action": "triage",
                }
            ],
        },
    )


def fake_agent_result(structured_result: JSONValue, **locks: JSONValue) -> JSONValue:
    result = AgentRunResult(
        structured_result=structured_result,
        result_digest=canonical_digest(structured_result),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    payload: dict[str, JSONValue] = {
        "agent_result": result.model_dump(mode="json"),
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "capability_leafs": [LEAF],
        "owned_evidence_ids": [_OWNED],
        "evidence_bundle_digest": EVIDENCE_REF,
        "artifact_paths": [],
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "mapping_digest": HEX_A,
        "issue_digest": HEX_B,
    }
    payload.update(locks)
    return payload


def skill_input() -> JSONValue:
    return {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "capability_leafs": [LEAF],
        "artifact_paths": ["inspect/observations.json"],
        "owned_evidence_ids": [_OWNED],
        "evidence_bundle_digest": EVIDENCE_REF,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "mapping_digest": HEX_A,
        "issue_digest": HEX_B,
    }


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


@pytest.mark.asyncio
async def test_issue_finalize_rejects_candidate_without_owned_evidence(tmp_path: Path) -> None:
    outcome = await execute_task(
        IssueAnalysisFinalizeHandler(),
        fake_agent_result(issue_candidate(evidence_ids=["missing"])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_issue_finalize_stamps_candidate_digest_for_owned_evidence(tmp_path: Path) -> None:
    structured = issue_candidate(evidence_ids=[_OWNED])
    outcome = await execute_task(
        IssueAnalysisFinalizeHandler(),
        fake_agent_result(structured),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["status"] == "completed"
    assert payload["candidate_digest"].startswith("sha256:")
    assert payload["candidates"][0]["observation_ids"] == [_OWNED]


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    first = await execute_task(
        IssueAnalysisPrepareHandler(),
        skill_input(),
        tmp_path,
        binding_data=BINDING,
    )
    second = await execute_task(
        IssueAnalysisPrepareHandler(),
        skill_input(),
        tmp_path,
        binding_data=BINDING,
    )
    assert first.status == "succeeded"
    request = AgentRunRequest.model_validate(first.output)
    assert request.canonical_bytes() == AgentRunRequest.model_validate(second.output).canonical_bytes()
    skill, persona, business = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned issue-analyzer skill" in (skill.text_content or "")
    assert "Quality explorer persona" in (persona.text_content or "")
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
        FactBaselinePrepareHandler(),
        skill_input(),
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
        (FactBaselinePrepareHandler(), "Capability-owned fact-baseline skill"),
        (InspectPrepareHandler(), "Capability-owned inspect skill"),
        (IssueTriagePrepareHandler(), "Capability-owned issue-triage skill"),
        (ReportPrepareHandler(), "Capability-owned report-generator skill"),
    ),
)
async def test_each_prepare_locks_skill_persona_and_execution(
    handler: TaskHandler, marker: str, tmp_path: Path
) -> None:
    outcome = await execute_task(handler, skill_input(), tmp_path, binding_data=BINDING)
    assert outcome.status == "succeeded"
    request = AgentRunRequest.model_validate(outcome.output)
    assert marker in (request.instructions[0].text_content or "")
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_fact_baseline_finalize_rejects_unowned_source_evidence(tmp_path: Path) -> None:
    structured = {
        "source": "seed_file",
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "warnings": [],
        "facts": {"route_prefix": "/api/v1"},
        "source_evidence_ids": ["EVID-missing"],
    }
    outcome = await execute_task(
        FactBaselineFinalizeHandler(),
        fake_agent_result(cast(JSONValue, structured)),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_inspect_finalize_rejects_unclosed_projection_digest(tmp_path: Path) -> None:
    structured = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": "c" * 64,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
    }
    outcome = await execute_task(
        InspectFinalizeHandler(),
        fake_agent_result(cast(JSONValue, structured)),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_issue_triage_finalize_rejects_unauthenticated_evidence_digest(tmp_path: Path) -> None:
    structured = {
        "schema_version": "1.0",
        "problem_id": "PROB-1",
        "expected_problem_version": 1,
        "review_id": "REV-1",
        "change_id": CHANGE_ID,
        "evidence_digests": {"inspect/observations.json": "sha256:" + ("f" * 64)},
        "recommended_action": "confirm_assessment",
        "reason": "x",
        "summary": "y",
        "reasoning": "z",
    }
    outcome = await execute_task(
        IssueTriageFinalizeHandler(),
        fake_agent_result(
            cast(JSONValue, structured),
            locked_evidence_digests={"inspect/observations.json": EVIDENCE_REF},
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_report_finalize_rejects_unreferenced_projection(tmp_path: Path) -> None:
    structured = {
        "schema_version": "1.1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "mapping_digest": HEX_A,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "issue_digest": HEX_B,
        "metrics_digest": "d" * 64,
        "risk_rationale": "refined",
        "recommendation": "Do not release: failing tests or hard blockers must be resolved first.",
    }
    outcome = await execute_task(
        ReportFinalizeHandler(),
        fake_agent_result(cast(JSONValue, structured)),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


def test_quality_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        "skills/aa-fact-baseline/SKILL.md",
        "skills/aa-inspect/SKILL.md",
        "skills/aa-issue-analyzer/SKILL.md",
        "skills/aa-issue-triage-advisor/SKILL.md",
        "skills/aa-report-generator/SKILL.md",
        "skills/aa-dashboard/SKILL.md",
        "personas/explorer.md",
        "personas/reviewer.md",
        "personas/reporter.md",
        "result-contracts/fact-baseline.v1.schema.json",
        "result-contracts/inspection.v1.schema.json",
        "result-contracts/issue-analysis.v1.schema.json",
        "result-contracts/issue-triage.v1.schema.json",
        "result-contracts/report.v1.schema.json",
    )
    missing = [item for item in required if not (_RESOURCES / item).is_file()]
    assert missing == [], f"missing quality resources: {missing}"
    hits: list[str] = []
    for path in _resource_files():
        if path.suffix == ".json":
            continue
        if _TOKEN.search(path.read_text(encoding="utf-8")):
            hits.append(path.relative_to(_RESOURCES).as_posix())
    assert hits == [], f"forbidden provider/legacy tokens in resources: {hits}"
    lowered = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in _resource_files() if path.suffix != ".json"
    )
    for token in _FORBIDDEN:
        assert token not in lowered
    explorer = (_RESOURCES / "personas/explorer.md").read_text(encoding="utf-8")
    reviewer = (_RESOURCES / "personas/reviewer.md").read_text(encoding="utf-8")
    reporter = (_RESOURCES / "personas/reporter.md").read_text(encoding="utf-8")
    assert "Quality explorer persona" in explorer
    assert "Quality reviewer persona" in reviewer
    assert "Quality reporter persona" in reporter
    assert "Document-author persona" not in explorer
    assert "Generation reviewer" not in reviewer


def test_result_contracts_match_capability_schemas() -> None:
    assert resource_bytes("result-contracts/fact-baseline.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, FactBaselineResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/inspection.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, InspectionResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/issue-analysis.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, IssueAnalysisResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/issue-triage.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, IssueTriageResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/report.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ReportResultV1.model_json_schema())
    )


def test_quality_plugin_has_no_product_hooks_import() -> None:
    from quality_fixtures import production_import_roots  # pyright: ignore[reportMissingImports]

    names = production_import_roots()
    assert "ProductHooks" not in names
    assert "assurance_agent" not in names
    assert "assurance_kernel" not in names
    assert "agent_runtime_opencode" not in names
    assert "agent_runtime_cursor" not in names
