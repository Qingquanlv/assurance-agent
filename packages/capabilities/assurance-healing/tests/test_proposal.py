from __future__ import annotations

import re
import hashlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from tests.product.test_change_local_output_routing import dual_roots, execute_task

from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.operations.proposal import (
    AllocateHealingAttemptHandler,
    CoverageRepairFinalizeHandler,
    CoverageRepairPrepareHandler,
    FixProposalFinalizeHandler,
    FixProposalPrepareHandler,
    RecordCodegenFixApplyHandler,
    RecordFixerApprovalHandler,
)
from assurance_healing.resource_loader import resource_bytes
from healing_fixtures import as_object  # pyright: ignore[reportMissingImports]

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_healing" / "resources"
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
_SHA = "a" * 64
BINDING: dict[str, Any] = {
    "agent_profile": "aa-doc-author",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def proposal_input() -> dict[str, Any]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "owner_id": "assurance.healing",
        "capability_leafs": ["entities.item.create"],
        "allowed_paths": ["tests/api/test_users.py"],
        "allowed_roots": ["tests/"],
        "baseline_digest": "b" * 64,
        "candidate_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "mapping_paths": ["tests/api/test_users.py"],
        "require_approval": True,
        "execution_evidence_digest": "e" * 64,
    }


def valid_proposal() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "summary": {"eligible_count": 1},
        "proposals": [
            {
                "proposal_id": "P1",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "needs_review": False,
                "files_to_modify": ["tests/api/test_users.py"],
            }
        ],
    }


def fake_agent_result(structured: dict[str, Any], **extra: Any) -> dict[str, Any]:
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest
    from tests.capabilities.agent_harness import FakeAgentAdapter

    payload = cast(JSONValue, structured)
    result = AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    prepare = proposal_input()
    body = {
        "agent_result": result.model_dump(mode="json"),
        **prepare,
        "prepare": prepare,
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_fix_proposal_prepare_is_deterministic_and_provider_neutral(tmp_path: Path) -> None:
    first = await execute_task(FixProposalPrepareHandler(), proposal_input(), tmp_path, binding_data=BINDING)
    second = await execute_task(FixProposalPrepareHandler(), proposal_input(), tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    left = AgentRunRequest.model_validate(first.output)
    right = AgentRunRequest.model_validate(second.output)
    assert left.canonical_bytes() == right.canonical_bytes()
    skill, business = left.instructions
    assert skill.media_type == "text/plain"
    assert business.media_type == "application/json"
    encoded = left.canonical_bytes().decode("utf-8").lower()
    assert "sort_keys=true" in encoded
    assert "ensure_ascii=false" in encoded
    assert "allow_nan=false" in encoded
    assert "exactly one trailing newline" in encoded
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded


@pytest.mark.asyncio
async def test_fix_proposal_authenticates_and_receives_issue_analysis(tmp_path: Path) -> None:
    relative = "qa/results/inspect/issue-analysis.json"
    data = b'{"reason":"wrong database binding"}'
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    ref = {"path": relative, "digest": hashlib.sha256(data).hexdigest()}
    request = {**proposal_input(), "issue_analysis_ref": ref}
    prepared = await execute_task(FixProposalPrepareHandler(), request, tmp_path, binding_data=BINDING)
    assert prepared.status == "succeeded", prepared.failure
    instructions = AgentRunRequest.model_validate(prepared.output).instructions
    content = instructions[-1].json_content
    assert isinstance(content, Mapping)
    assert content["issue_analysis_ref"] == ref
    path.write_bytes(b"{}")
    changed = await execute_task(FixProposalPrepareHandler(), request, tmp_path, binding_data=BINDING)
    assert changed.status == "failed"


@pytest.mark.asyncio
async def test_fix_proposal_finalize_rejects_nonexistent_file(tmp_path: Path) -> None:
    raw = valid_proposal()
    raw["proposals"][0]["files_to_modify"] = ["tests/api/missing.py"]  # type: ignore[index]
    outcome = await execute_task(FixProposalFinalizeHandler(), fake_agent_result(raw), tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_fix_proposal_finalize_rejects_unknown_capability(tmp_path: Path) -> None:
    extra = {**fake_agent_result(valid_proposal()), "claimed_capabilities": ["ghost.capability"]}
    outcome = await execute_task(FixProposalFinalizeHandler(), extra, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_fix_proposal_finalize_accepts_typed_proposal(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    target = project / "tests/api/test_users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert True\n")
    staged = write_root / "qa/results/healing/fix-proposal.json"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(canonical_json_bytes(cast(JSONValue, valid_proposal())) + b"\n")
    outcome = await execute_task(
        FixProposalFinalizeHandler(),
        fake_agent_result(valid_proposal()),
        project,
        write_root=write_root,
    )
    assert outcome.status == "succeeded"
    assert as_object(outcome.output)["proposals"][0]["proposal_id"] == "P1"
    assert not (write_root / "tests/api/test_users.py").exists()


@pytest.mark.asyncio
async def test_fix_proposal_finalize_rejects_wrapped_runtime_input(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    target = project / "tests/api/test_users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert True\n")
    proposal = valid_proposal()
    proposal_path = write_root / "qa/results/healing/fix-proposal.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_bytes(canonical_json_bytes(cast(JSONValue, proposal)) + b"\n")
    current = fake_agent_result(proposal)

    outcome = await execute_task(
        FixProposalFinalizeHandler(),
        {
            "agent_result": current["agent_result"],
            "prepared": {},
            "validated_input": proposal_input(),
        },
        project,
        write_root=write_root,
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_fix_proposal_finalize_rejects_rewritten_baseline_digest(tmp_path: Path) -> None:
    (tmp_path / "tests/api").mkdir(parents=True)
    (tmp_path / "tests/api/test_users.py").write_text("def test_ok():\n    assert True\n")
    extra = fake_agent_result(valid_proposal())
    extra["baseline_digest"] = "f" * 64
    outcome = await execute_task(FixProposalFinalizeHandler(), extra, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_allocate_returns_effect_intent_without_writing(tmp_path: Path) -> None:
    payload = {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "owner_id": "assurance.healing",
        "attempt_number": 1,
        "source_batch_id": "batch-1",
        "entry_batch_id": "batch-1",
        "candidate_digest": "c" * 64,
        "baseline_digest": "b" * 64,
        "policy_digest": "d" * 64,
        "execution_evidence_digest": "e" * 64,
        "prior_operation_ids": [],
    }
    outcome = await execute_task(AllocateHealingAttemptHandler(), payload, tmp_path)
    assert outcome.status == "succeeded"
    assert outcome.effects
    assert outcome.effects[0].kind == "assurance.healing.effect.allocation.v2"
    assert list((tmp_path / "healing").glob("*")) == [] if (tmp_path / "healing").exists() else True
    assert as_object(outcome.output)["baseline_embedded"] is True


@pytest.mark.asyncio
async def test_record_approval_and_apply_emit_intents_only(tmp_path: Path) -> None:
    approval = await execute_task(
        RecordFixerApprovalHandler(),
        {
            "change_id": "CH-DEMO-001",
            "plan_digest": _SHA,
            "plan_ref": {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            },
            "owner_id": "assurance.healing",
            "root_invocation_id": "inv-1",
            "interrupt_task_id": "task-1",
            "source_gate_attempt_id": "gate-1",
            "source_tree_id": "tree-src",
            "target_tree_id": "tree-dst",
            "proposal_digest": "a" * 64,
            "fixer_authority_digest": "b" * 64,
            "candidate_digest": "c" * 64,
            "baseline_digest": "d" * 64,
            "policy_digest": "e" * 64,
            "targets": ["api"],
            "paths": ["tests/api/test_users.py"],
        },
        tmp_path,
    )
    assert approval.status == "succeeded"
    assert approval.effects[0].kind == "assurance.healing.effect.proposal-approved.v1"
    apply = await execute_task(
        RecordCodegenFixApplyHandler(),
        {
            "change_id": "CH-DEMO-001",
            "plan_digest": _SHA,
            "plan_ref": {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            },
            "owner_id": "assurance.healing",
            "target": "api",
            "entry_batch_id": "batch-1",
            "outcome": "applied",
            "candidate_digest": "a" * 64,
            "baseline_digest": "b" * 64,
            "policy_digest": "c" * 64,
            "write_set_id": "ws-1",
            "proposal_ids": ["P1"],
            "claimed_modified_paths": ["tests/api/test_users.py"],
            "safety_payload_digest": "e" * 64,
        },
        tmp_path,
    )
    assert apply.status == "succeeded"
    assert apply.effects[0].kind == "assurance.healing.effect.heal-apply.v2"


@pytest.mark.asyncio
async def test_coverage_repair_prepare_and_finalize(tmp_path: Path) -> None:
    brief = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "batch_id": "batch-1",
        "probe_verdict": "pass",
        "eligible": True,
        "allowed_test_files": ["tests/api/test_users.py"],
        "repair_items": [
            {
                "kind": "uncovered_required_case",
                "locator": {"case_id": "TC_A"},
                "metric": "case_coverage",
            }
        ],
    }
    prepared = await execute_task(
        CoverageRepairPrepareHandler(),
        {
            "change_id": "CH-DEMO-001",
            "brief": brief,
            "baseline_digest": "b" * 64,
            "allowed_roots": ["tests/"],
        },
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    summary = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "attempt": 1,
        "attempt_token": "token-1",
        "applied": True,
        "files_modified": ["tests/api/test_users.py"],
        "addressed_items": ["TC_A"],
    }
    project, write_root = dual_roots(tmp_path)
    target = project / "tests/api/test_users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert True\n")
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest
    from tests.capabilities.agent_harness import FakeAgentAdapter

    payload = cast(JSONValue, summary)
    result = AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    repair_prepare = {
        "change_id": "CH-DEMO-001",
        "brief": brief,
        "baseline_digest": "b" * 64,
        "allowed_roots": ["tests/"],
    }
    finalized = await execute_task(
        CoverageRepairFinalizeHandler(),
        {
            "agent_result": result.model_dump(mode="json"),
            "change_id": "CH-DEMO-001",
            "brief": brief,
            "baseline_digest": "b" * 64,
            "allowed_roots": ["tests/"],
            "artifact_paths": ["tests/api/test_users.py"],
            "prepare": repair_prepare,
        },
        project,
        write_root=write_root,
    )
    assert finalized.status == "succeeded"


@pytest.mark.asyncio
async def test_coverage_repair_finalize_rejects_unknown_locator(tmp_path: Path) -> None:
    brief = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "batch_id": "batch-1",
        "probe_verdict": "pass",
        "eligible": True,
        "allowed_test_files": ["tests/api/test_users.py"],
        "repair_items": [
            {
                "kind": "uncovered_required_case",
                "locator": {"case_id": "TC_A"},
                "metric": "case_coverage",
            }
        ],
    }
    summary = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "attempt": 1,
        "attempt_token": "token-1",
        "applied": True,
        "files_modified": ["tests/api/test_users.py"],
        "addressed_items": ["NOT_IN_BRIEF"],
    }
    (tmp_path / "tests/api").mkdir(parents=True)
    (tmp_path / "tests/api/test_users.py").write_text("def test_ok():\n    assert True\n")
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest
    from tests.capabilities.agent_harness import FakeAgentAdapter

    payload = cast(JSONValue, summary)
    result = AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    finalized = await execute_task(
        CoverageRepairFinalizeHandler(),
        {
            "agent_result": result.model_dump(mode="json"),
            "change_id": "CH-DEMO-001",
            "brief": brief,
            "baseline_digest": "b" * 64,
            "allowed_roots": ["tests/"],
            "artifact_paths": ["tests/api/test_users.py"],
            "prepare": {
                "change_id": "CH-DEMO-001",
                "brief": brief,
                "baseline_digest": "b" * 64,
                "allowed_roots": ["tests/"],
            },
        },
        tmp_path,
    )
    assert finalized.status == "failed"
    assert finalized.failure is not None
    assert finalized.failure.kind == "invalid_output"
    assert finalized.failure.retryable is True


@pytest.mark.asyncio
async def test_failed_proposal_validation_leaves_canonical_outputs_unchanged(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    canonical = project / "qa/results/healing/fix-proposal.json"
    canonical.parent.mkdir(parents=True)
    original = b'{"schema_version":"1"}\n'
    canonical.write_bytes(original)
    raw = valid_proposal()
    raw["proposals"][0]["files_to_modify"] = ["tests/api/missing.py"]  # type: ignore[index]
    outcome = await execute_task(
        FixProposalFinalizeHandler(),
        fake_agent_result(raw),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert canonical.read_bytes() == original


def test_healing_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        "skills/aa-fix-proposal/SKILL.md",
        "skills/aa-coverage-repair/SKILL.md",
        "skills/aa-apply-test-repair/SKILL.md",
        "result-contracts/fix-proposal.v1.schema.json",
        "result-contracts/coverage-repair.v1.schema.json",
        "result-contracts/applied-test-repair.v1.schema.json",
    )
    missing = [item for item in required if not (_RESOURCES / item).is_file()]
    assert missing == []
    hits = [
        path.relative_to(_RESOURCES).as_posix()
        for path in _resource_files()
        if path.suffix in {".md", ".json", ".txt"} and _TOKEN.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
    lowered = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in _resource_files()
        if path.suffix in {".md", ".json", ".txt"}
    )
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contracts_match_typed_models() -> None:
    from assurance_healing.contracts import CoverageRepairApplySummary
    from assurance_healing.contracts.application import TestRepairResultV1

    assert resource_bytes("result-contracts/fix-proposal.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, FixProposalResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/coverage-repair.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, CoverageRepairApplySummary.model_json_schema())
    )
    assert resource_bytes("result-contracts/applied-test-repair.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, TestRepairResultV1.model_json_schema())
    )
