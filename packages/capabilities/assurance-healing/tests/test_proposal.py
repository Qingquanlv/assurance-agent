from __future__ import annotations

import re
import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.artifacts import stage_json_artifact
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel, TaskHandler
from tests.product.test_change_local_output_routing import dual_roots, execute_task

from assurance_execution.contracts.workflow import EXECUTION_CYCLE_PATH, ExecutionCycleDocumentV1
from assurance_generation.contracts.workflow import GENERATION_CYCLE_PATH, GenerationCycleResultV1
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.issue_handoff import IssueAnalysisHandoffV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_healing.ops.fix_proposal import (
    finalize as fix_proposal_finalize,
    prepare as fix_proposal_prepare,
)
from healing_fixtures import as_object  # pyright: ignore[reportMissingImports]

_PACKAGE = Path(__file__).resolve().parent.parent / "assurance_healing"
_RESOURCES = _PACKAGE / "resources"
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
    for root in (_RESOURCES, _PACKAGE / "ops"):
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".py":
                yield path


_POLICY = {
    "resource_id": "assurance.product.configuration.product-policy",
    "sha256": "d" * 64,
}
_EXECUTION_RECEIPT = {"receipt_id": "execute", "receipt_digest": "c" * 64}


def _plan_ref() -> dict[str, str]:
    return {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }


def _stage_cycle(workspace: Path, relative: str, document: FrozenModel) -> dict[str, str]:
    path = workspace / relative
    if path.exists():
        path.unlink()
    ref = stage_json_artifact(workspace, relative, document)
    return {"path": ref.path, "digest": ref.digest}


def _stage_repair_cycles(
    workspace: Path,
    *,
    change_id: str = "CH-DEMO-001",
    coverage_epoch: int = 0,
    repair_round: int = 1,
    source_refs: list[dict[str, str]] | None = None,
    evidence_digest: str = "b" * 64,
) -> tuple[dict[str, str], dict[str, str]]:
    plan = _plan_ref()
    sources = source_refs or [{"path": "tests/api/test_users.py", "digest": _SHA}]
    mapping_ref = {"path": "qa/results/generated/mapping.json", "digest": _SHA}
    generation = GenerationCycleResultV1.model_validate(
        {
            "change_id": change_id,
            "coverage_epoch": coverage_epoch,
            "reviewed_case": {
                "change_id": change_id,
                "coverage_epoch": coverage_epoch,
                "plan_digest": _SHA,
                "plan_ref": plan,
                "preparation_refs": [plan],
                "case_refs": [{"path": "qa/cases/items/case.yaml", "digest": _SHA}],
                "review_ref": {"path": "qa/results/review/case-review.json", "digest": _SHA},
                "selection_ref": {
                    "path": f"qa/results/cases/epochs/{coverage_epoch}/selection.json",
                    "digest": _SHA,
                },
            },
            "plan_digest": _SHA,
            "plan_ref": plan,
            "mapping_ref": mapping_ref,
            "source_refs": sources,
            "plan_refs": [plan],
            "method_plan_ref": {
                "path": f"qa/results/generation/epochs/{coverage_epoch}/obligation-methods.json",
                "digest": _SHA,
            },
        }
    )
    cycle_sources = [item for item in sources if item["path"].startswith("qa/")] or [
        {"path": "qa/tests/api/test_users.py", "digest": _SHA}
    ]
    execution = ExecutionCycleDocumentV1.model_validate(
        {
            "change_id": change_id,
            "plan_digest": _SHA,
            "plan_ref": plan,
            "coverage_epoch": coverage_epoch,
            "repair_round": repair_round,
            "batch_id": "batch-1",
            "executed_at": datetime(2026, 9, 5, 12, 0, 1, tzinfo=UTC),
            "final_status": "FAIL",
            "evidence_ref": {
                "path": "qa/results/execution/execute-result.json",
                "digest": evidence_digest,
            },
            "mapping_ref": mapping_ref,
            "source_refs": cycle_sources,
            "family_outcomes": [{"family": "api", "state": "executed"}],
        }
    )
    return (
        _stage_cycle(workspace, GENERATION_CYCLE_PATH, generation),
        _stage_cycle(workspace, EXECUTION_CYCLE_PATH, execution),
    )


def proposal_input(workspace: Path, **overrides: Any) -> dict[str, Any]:
    coverage_epoch = int(overrides.get("coverage_epoch", 0))
    repair_round = int(overrides.get("repair_round", 1))
    generation_ref, execution_ref = _stage_repair_cycles(
        workspace,
        coverage_epoch=coverage_epoch,
        repair_round=repair_round,
    )
    payload: dict[str, Any] = {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": _plan_ref(),
        "capability_leafs": ["entities.item.create"],
        "coverage_epoch": coverage_epoch,
        "repair_round": repair_round,
        "product_policy": _POLICY,
        "generation_ref": generation_ref,
        "execution_ref": execution_ref,
        "execution_receipt": _EXECUTION_RECEIPT,
    }
    payload.update(overrides)
    return payload


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


def fake_agent_result(
    workspace: Path,
    structured: dict[str, Any],
    *,
    prepare: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.wire.schema import canonical_digest
    from tests.capabilities.agent_harness import FakeAgentAdapter

    payload = cast(JSONValue, structured)
    result = AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    bound = proposal_input(workspace) if prepare is None else prepare
    body = {
        "agent_result": result.model_dump(mode="json"),
        **bound,
        "prepare": bound,
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_fix_proposal_prepare_is_deterministic_and_provider_neutral(tmp_path: Path) -> None:
    first = await execute_task(
        cast(TaskHandler, fix_proposal_prepare), proposal_input(tmp_path), tmp_path, binding_data=BINDING
    )
    second = await execute_task(
        cast(TaskHandler, fix_proposal_prepare), proposal_input(tmp_path), tmp_path, binding_data=BINDING
    )
    assert first.status == "succeeded"
    left = AgentRunRequest.model_validate(first.output)
    right = AgentRunRequest.model_validate(second.output)
    assert left.canonical_bytes() == right.canonical_bytes()
    skill, business = left.instructions
    assert skill.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert isinstance(business.json_content, Mapping)
    assert "require_approval" not in business.json_content
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
    analysis_ref = EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())
    handoff_ref = stage_json_artifact(
        tmp_path,
        "qa/results/healing/issue-analysis-handoff.json",
        IssueAnalysisHandoffV1(coverage_epoch=0, issue_analysis_ref=analysis_ref),
    )
    request = {
        **proposal_input(tmp_path),
        "issue_analysis_handoff_ref": handoff_ref.model_dump(mode="json"),
    }
    prepared = await execute_task(
        cast(TaskHandler, fix_proposal_prepare), request, tmp_path, binding_data=BINDING
    )
    assert prepared.status == "succeeded", prepared.failure
    instructions = AgentRunRequest.model_validate(prepared.output).instructions
    content = instructions[-1].json_content
    assert isinstance(content, Mapping)
    assert content["issue_analysis_ref"] == analysis_ref.model_dump(mode="json")
    path.write_bytes(b"{}")
    changed = await execute_task(
        cast(TaskHandler, fix_proposal_prepare), request, tmp_path, binding_data=BINDING
    )
    assert changed.status == "failed"


@pytest.mark.asyncio
async def test_fix_proposal_keeps_issue_analysis_only_for_the_current_epoch(tmp_path: Path) -> None:
    analysis = b'{"reason":"wrong database binding"}'
    analysis_path = tmp_path / "qa/results/inspect/issue-analysis.json"
    analysis_path.parent.mkdir(parents=True)
    analysis_path.write_bytes(analysis)
    analysis_ref = EvidenceArtifactRefV1(
        path="qa/results/inspect/issue-analysis.json",
        digest=hashlib.sha256(analysis).hexdigest(),
    )
    handoff_ref = stage_json_artifact(
        tmp_path,
        "qa/results/healing/issue-analysis-handoff.json",
        IssueAnalysisHandoffV1(coverage_epoch=1, issue_analysis_ref=analysis_ref),
    )
    handoff = handoff_ref.model_dump(mode="json")
    analysis_dump = analysis_ref.model_dump(mode="json")

    async def prepared(extra: dict[str, Any]) -> Mapping[str, object]:
        outcome = await execute_task(
            cast(TaskHandler, fix_proposal_prepare),
            cast(JSONValue, {**proposal_input(tmp_path), **extra}),
            tmp_path,
            binding_data=BINDING,
        )
        assert outcome.status == "succeeded", outcome.failure
        content = AgentRunRequest.model_validate(outcome.output).instructions[-1].json_content
        assert isinstance(content, Mapping)
        return content

    current = await prepared({"coverage_epoch": 1, "issue_analysis_handoff_ref": handoff})
    assert current["issue_analysis_ref"] == analysis_dump

    stale = await prepared(
        {
            "coverage_epoch": 2,
            "issue_analysis_handoff_ref": handoff,
        }
    )
    assert stale.get("issue_analysis_ref") is None


@pytest.mark.asyncio
async def test_fix_proposal_finalize_rejects_nonexistent_file(tmp_path: Path) -> None:
    raw = valid_proposal()
    raw["proposals"][0]["files_to_modify"] = ["tests/api/missing.py"]  # type: ignore[index]
    outcome = await execute_task(
        cast(TaskHandler, fix_proposal_finalize), fake_agent_result(tmp_path, raw), tmp_path
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


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
        cast(TaskHandler, fix_proposal_finalize),
        fake_agent_result(project, valid_proposal()),
        project,
        write_root=write_root,
    )
    assert outcome.status == "succeeded"
    assert as_object(outcome.output)["proposals"][0]["proposal_id"] == "P1"
    assert not (write_root / "tests/api/test_users.py").exists()


@pytest.mark.asyncio
async def test_fix_proposal_finalize_accepts_the_product_envelope(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    target = project / "tests/api/test_users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert True\n")
    staged = write_root / "qa/results/healing/fix-proposal.json"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(canonical_json_bytes(cast(JSONValue, valid_proposal())) + b"\n")
    full = fake_agent_result(project, valid_proposal())
    outcome = await execute_task(
        cast(TaskHandler, fix_proposal_finalize),
        {"prepare": full["prepare"], "agent_result": full["agent_result"]},
        project,
        write_root=write_root,
    )
    assert outcome.status == "succeeded"
    assert as_object(outcome.output)["proposals"][0]["proposal_id"] == "P1"


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
    current = fake_agent_result(project, proposal)

    outcome = await execute_task(
        cast(TaskHandler, fix_proposal_finalize),
        {
            "agent_result": current["agent_result"],
            "prepared": {},
            "validated_input": proposal_input(project),
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
    staged = tmp_path / "qa/results/healing/fix-proposal.json"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(canonical_json_bytes(cast(JSONValue, valid_proposal())) + b"\n")
    prepare = proposal_input(tmp_path)
    cycle = tmp_path / EXECUTION_CYCLE_PATH
    rewritten = ExecutionCycleDocumentV1.model_validate_json(cycle.read_bytes()).model_copy(
        update={
            "evidence_ref": EvidenceArtifactRefV1(
                path="qa/results/execution/execute-result.json",
                digest="f" * 64,
            )
        }
    )
    cycle.write_bytes(canonical_json_bytes(cast(JSONValue, rewritten.model_dump(mode="json"))) + b"\n")
    extra = fake_agent_result(tmp_path, valid_proposal(), prepare=prepare)
    outcome = await execute_task(cast(TaskHandler, fix_proposal_finalize), extra, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


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
        cast(TaskHandler, fix_proposal_finalize),
        fake_agent_result(project, raw),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert canonical.read_bytes() == original


def test_healing_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        _PACKAGE / "ops/fix_proposal/SKILL.md",
        _PACKAGE / "ops/apply_test_repair/SKILL.md",
    )
    missing = [item for item in required if not item.is_file()]
    assert missing == []
    hits = [
        path.relative_to(_PACKAGE).as_posix()
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
    from assurance_healing.contracts.application import TestRepairResultV1
    from assurance_healing.ops import router

    expected = {
        "apply-test-repair": TestRepairResultV1,
        "fix-proposal": FixProposalResultV1,
    }
    assert {name: _symbol(op.agent.result) for name, op in router.agent_ops().items()} == {
        name: _symbol(model) for name, model in expected.items()
    }


def _symbol(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"
