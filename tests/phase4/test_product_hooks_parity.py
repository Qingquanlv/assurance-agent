from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.models import SourceFile, SourceKind, SourceSnapshot
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    EffectIntent,
    ResourceClaims,
    ValidationContext,
)

from assurance_healing.contracts.safety import HealingOverrideTokenV1, TestChangePolicyV1
from assurance_healing.contracts.wire import override_token_digest
from assurance_healing.effects.allocation import HealingAllocationEffect
from assurance_healing.effects.apply import HealApplyEffect
from assurance_healing.effects.approval import ProposalApprovedEffect
from assurance_healing.effects.store import InMemoryHealingStore
from assurance_healing.operations.keys import (
    derive_allocation_ids,
    derive_approval_id,
    derive_heal_record_key,
)
from assurance_healing.operations.status import ProjectEpisodeHandler
from assurance_healing.plugin import HEALING_EFFECT_IDS, HealingPlugin
from assurance_healing.resource_loader import resource_bytes
from assurance_healing.validators.override import OverrideValidator
from assurance_healing.validators.test_tree import TestTreeValidator
from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.contracts.review import ImprovementReviewSubject
from assurance_improvement.operations.agent import ImprovementReviewFinalizeHandler, RetroFinalizeHandler
from assurance_kernel.workflow.core.product_hooks import ProductHooks
from assurance_kernel.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
    EffectRegistry,
)
from assurance_quality.contracts.issues import IssueCandidateDocument
from assurance_quality.operations.agent_skills import IssueAnalysisFinalizeHandler
from assurance_quality.operations.identity import candidate_document_digest as quality_candidate_digest
from tests.phase4.conformance import execute_task
from tests.phase4.ownership import OWNERSHIP_PATH, load_ownership_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_WHEEL_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "packages" / "assurance-intake",
    REPO_ROOT / "packages" / "assurance-generation",
    REPO_ROOT / "packages" / "assurance-execution",
    REPO_ROOT / "packages" / "assurance-healing",
    REPO_ROOT / "packages" / "assurance-quality",
    REPO_ROOT / "packages" / "assurance-improvement",
)
CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "product-hooks-cases.yaml"
_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64
_HEX_D = "d" * 64
_HEX_E = "e" * 64
_CHANGE_ID = "CH-DEMO-001"
_BATCH_ID = "20260822T000000Z"
_EVIDENCE = f"sha256:{_HEX_A}"
_FORBIDDEN_TYPE_NAMES = frozenset({"Hooks", "HookRegistry", "ProductRuntime", "SemanticPins"})
_REGISTRY_KINDS = frozenset(
    {
        "TaskHandler",
        "CommitValidator",
        "DurableEffectHandler",
        "EffectRegistration",
    }
)
_EFFECT_KIND_MAP = {
    HEALING_ALLOCATION_V2: "assurance.healing.effect.allocation.v2",
    FIXER_PROPOSAL_APPROVED_V1: "assurance.healing.effect.proposal-approved.v1",
    HEAL_RECORD_APPLY_V2: "assurance.healing.effect.heal-apply.v2",
}
_DEFAULT_PRODUCT_ROOTS = frozenset({"app", "src", "web/src"})


def forbidden_symbol_scan(roots: Sequence[Path], symbols: Sequence[str]) -> set[str]:
    wanted = frozenset(symbols)
    found: set[str] = set()
    for root in roots:
        package = root / root.name.replace("-", "_")
        for path in package.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in wanted:
                    found.add(node.id)
                elif isinstance(node, ast.Attribute) and node.attr in wanted:
                    found.add(node.attr)
                elif isinstance(node, ast.alias) and (node.name in wanted or node.asname in wanted):
                    found.add(node.name if node.name in wanted else str(node.asname))
    return found


def load_hook_cases() -> tuple[dict[str, object], ...]:
    raw = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "1":
        raise AssertionError("product-hooks-cases.yaml must use schema_version 1")
    rows = raw.get("hooks")
    if not isinstance(rows, list):
        raise AssertionError("product-hooks-cases.yaml must contain a hooks list")
    parsed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AssertionError("each hook case must be a mapping")
        parsed.append({str(key): value for key, value in row.items()})
    return tuple(parsed)


def test_every_product_hook_has_one_verified_replacement() -> None:
    legacy_fields = {field.name for field in dataclasses.fields(ProductHooks)}
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    migrate = {
        item.legacy_id for item in ledger.items if item.kind == "hook" and item.disposition == "migrate"
    }
    verified = {item.legacy_id for item in ledger.items if item.kind == "hook" and item.status == "verified"}
    pins = next(item for item in ledger.items if item.kind == "hook" and item.legacy_id == "semantic_pins")
    assert migrate == verified
    assert migrate | {pins.legacy_id} == legacy_fields
    assert pins.disposition == "delete_phase6"
    assert pins.owner is None
    assert pins.new_id is None
    assert pins.status == "planned"
    assert pins.verification is None


def test_no_new_wheel_imports_or_recreates_product_hooks() -> None:
    assert (
        forbidden_symbol_scan(
            NEW_WHEEL_ROOTS,
            symbols=("ProductHooks", "install_product_hooks", "current_product_hooks", "semantic_pins"),
        )
        == set()
    )


def test_fixture_covers_every_legacy_hook_field() -> None:
    rows = load_hook_cases()
    names = [str(row["hook"]) for row in rows]
    expected = [field.name for field in dataclasses.fields(ProductHooks)]
    assert names == expected or set(names) == set(expected)
    assert len(names) == len(set(names)) == 18
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    by_id = {item.legacy_id: item for item in ledger.items if item.kind == "hook"}
    for row in rows:
        item = by_id[str(row["hook"])]
        owner = row["owner"]
        new_id = row["new_id"]
        assert item.owner == owner
        assert item.new_id == new_id
        assert "compare" in row
        assert "input" in row


def test_no_hidden_catchall_hook_registry() -> None:
    assert _catchall_scan(NEW_WHEEL_ROOTS) == set()


async def test_legacy_and_new_hook_paths_match_on_fixture(tmp_path: Path) -> None:
    for row in load_hook_cases():
        hook = str(row["hook"])
        await _compare_hook(hook, tmp_path / hook)


async def _compare_hook(hook: str, tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    comparers = {
        "load_product_code_roots": _compare_product_roots,
        "candidate_document_digest": _compare_candidate_digest,
        "commit_healing_allocation_ledger": _compare_allocation_commit,
        "complete_issue_analyzer_outputs": _compare_issue_complete,
        "complete_improvement_reviewer_outputs": _compare_review_complete,
        "complete_signal_outputs": _compare_signal_complete,
        "complete_candidate_outputs": _compare_candidate_complete,
        "register_healing_effects": _compare_effect_registrations,
        "project_healing_episode": _compare_episode,
        "assert_test_tree_unchanged_or_healing": _compare_test_tree,
        "assert_test_changes_override_allowed": _compare_override_allowed,
        "build_test_changes_override_token": _compare_build_token,
        "load_test_changes_override_policy": _compare_policy,
        "token_json_bytes": _compare_token_bytes,
        "reconcile_healing_allocation": _compare_reconcile_allocation,
        "reconcile_fixer_proposal_approved": _compare_reconcile_approval,
        "reconcile_heal_record_apply": _compare_reconcile_apply,
        "semantic_pins": _compare_semantic_pins,
    }
    result = comparers[hook](tmp_path)
    if inspect.isawaitable(result):
        await result


def _compare_product_roots(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.safety import load_product_code_roots

    legacy = frozenset(load_product_code_roots(tmp_path))
    validator = TestTreeValidator()
    policy = TestChangePolicyV1.model_validate(
        json.loads(resource_bytes("policy/test-change-policy.v1.json"))
    )
    assert legacy == _DEFAULT_PRODUCT_ROOTS
    assert frozenset(validator._forbidden) == _DEFAULT_PRODUCT_ROOTS
    assert frozenset(policy.forbidden_product_roots) == _DEFAULT_PRODUCT_ROOTS


def _issue_candidate_document() -> dict[str, object]:
    return IssueCandidateDocument.model_validate(
        {
            "schema_version": "1.0",
            "change_id": _CHANGE_ID,
            "batch_id": _BATCH_ID,
            "evidence_bundle_digest": _EVIDENCE,
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": ["OBS-owned"],
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
        }
    ).model_dump(mode="json")


def _compare_candidate_digest(_tmp_path: Path) -> None:
    from assurance_agent.workflow.issues.identity import candidate_document_digest as legacy_digest

    document = _issue_candidate_document()
    assert legacy_digest(document) == quality_candidate_digest(document)


def _allocation_ids() -> dict[str, object]:
    return derive_allocation_ids(
        change_id=_CHANGE_ID,
        source_batch_id="batch-src",
        entry_batch_id="batch-src",
        candidate_digest=_HEX_A,
        attempt_number=1,
    )


def _allocation_intent() -> EffectIntent:
    ids = _allocation_ids()
    payload = {
        "schema_version": "2",
        "episode_id": str(ids["episode_id"]),
        "attempt_id": str(ids["attempt_id"]),
        "attempt_number": 1,
        "operation_id": str(ids["operation_id"]),
        "change_id": _CHANGE_ID,
        "owner_id": "assurance.healing",
        "source_batch_id": "batch-src",
        "entry_batch_id": "batch-src",
        "candidate_digest": _HEX_A,
        "baseline_digest": _HEX_B,
        "policy_digest": _HEX_C,
        "execution_evidence_digest": _HEX_D,
        "baseline_embedded": True,
    }
    return EffectIntent(kind="assurance.healing.effect.allocation.v2", payload=cast(JSONValue, payload))


async def _compare_allocation_commit(tmp_path: Path) -> None:
    from tests.helpers_aa import write_aa_config
    from assurance_agent.workflow.healing.allocation import commit_healing_allocation_ledger
    from assurance_agent.workflow.healing.operations import derive_allocation_ids as legacy_derive

    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / _CHANGE_ID
    (change_dir / "healing").mkdir(parents=True)
    (change_dir / "healing" / "fix-proposal.json").write_text('{"proposals": []}', encoding="utf-8")
    ids = _allocation_ids()
    first = commit_healing_allocation_ledger(
        change_dir,
        episode_id=str(ids["episode_id"]),
        attempt_id=str(ids["attempt_id"]),
        attempt_number=1,
        operation_id=str(ids["operation_id"]),
        source_batch_id="batch-src",
        baseline_sha256=_HEX_B,
        entry_batch_id="batch-src",
    )
    replay = commit_healing_allocation_ledger(
        change_dir,
        episode_id=str(ids["episode_id"]),
        attempt_id=str(ids["attempt_id"]),
        attempt_number=1,
        operation_id=str(ids["operation_id"]),
        source_batch_id="batch-src",
        baseline_sha256=_HEX_B,
        entry_batch_id="batch-src",
    )
    legacy_ids = legacy_derive(
        change_id=_CHANGE_ID,
        batch_id="batch-src",
        proposal_sha=_HEX_A,
        attempt_number=1,
    )
    handler = HealingAllocationEffect(store=InMemoryHealingStore())
    intent = _allocation_intent()
    key = str(ids["operation_id"])
    applied = await handler.apply(intent, key)
    second = await handler.apply(intent, key)
    assert first is True
    assert replay is False
    assert applied.status == "applied"
    assert second.status == "applied"
    assert second.receipt == applied.receipt
    assert str(ids["operation_id"]) == legacy_ids["operation_id"]
    assert as_object(applied.receipt)["operation_id"] == legacy_ids["operation_id"]


async def _compare_issue_complete(tmp_path: Path) -> None:
    from assurance_agent.workflow.issues.analyzer_output import complete_issue_analyzer_outputs
    from assurance_agent.workflow.issues.identity import candidate_document_digest as legacy_digest
    from tests.phase4.agent_harness import FakeAgentAdapter
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest as agent_digest

    document = _issue_candidate_document()
    change_dir = tmp_path / "change"
    inspect = change_dir / "inspect"
    inspect.mkdir(parents=True)
    (inspect / "issue-candidates.json").write_text(json.dumps(document), encoding="utf-8")
    status = {
        "schema_version": "1.0",
        "change_id": _CHANGE_ID,
        "batch_id": _BATCH_ID,
        "status": "completed",
        "evidence_bundle_digest": _EVIDENCE,
        "candidate_count": 1,
    }
    (inspect / "issue-analysis-status.json").write_text(json.dumps(status), encoding="utf-8")
    complete_issue_analyzer_outputs(
        change_dir,
        ("change:inspect/issue-candidates.json", "change:inspect/issue-analysis-status.json"),
    )
    stamped = json.loads((inspect / "issue-analysis-status.json").read_text(encoding="utf-8"))
    structured = {
        **document,
        "status": "completed",
        "candidate_count": 1,
    }
    result = AgentRunResult(
        structured_result=cast(JSONValue, structured),
        result_digest=agent_digest(cast(JSONValue, structured)),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    outcome = await execute_task(
        IssueAnalysisFinalizeHandler(),
        {
            "agent_result": result.model_dump(mode="json"),
            "change_id": _CHANGE_ID,
            "batch_id": _BATCH_ID,
            "capability_leafs": ["entities.item.create"],
            "owned_evidence_ids": ["OBS-owned"],
            "evidence_bundle_digest": _EVIDENCE,
            "artifact_paths": [],
            "execution_digest": _HEX_A,
            "healing_digest": _HEX_B,
            "trace_digest": _HEX_A,
            "coverage_digest": _HEX_B,
            "metrics_digest": _HEX_A,
            "case_digest": _HEX_A,
            "plan_digest": _HEX_B,
            "mapping_digest": _HEX_A,
            "issue_digest": _HEX_B,
        },
        tmp_path / "new",
    )
    assert outcome.status == "succeeded"
    new_digest = as_object(outcome.output)["candidate_digest"]
    assert stamped["candidate_digest"] == new_digest == legacy_digest(document)


async def _compare_review_complete(tmp_path: Path) -> None:
    from assurance_agent.workflow.improvements.review_subject import build_review_subject
    from assurance_agent.workflow.improvements.reviewer_output import complete_improvement_reviewer_outputs
    from tests.unit.workflow.improvements.test_reconcile_v3 import _candidate, _context
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest as agent_digest
    from tests.phase4.agent_harness import FakeAgentAdapter

    _subject, digest, data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")
    subject_path = tmp_path / "subject.json"
    assessment_path = tmp_path / "assessment.json"
    summary_path = tmp_path / "summary.md"
    subject_path.write_bytes(data)
    assessment_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "review_type": "improvement",
                "decision": "pass",
                "findings": [],
                "evidence_traceability": "complete",
                "scope_readiness": "ready",
                "verification_readiness": "ready",
                "delivery_safety": "ready",
                "human_review_required": False,
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text("# Review\n", encoding="utf-8")
    legacy = complete_improvement_reviewer_outputs(
        subject_path=subject_path,
        assessment_path=assessment_path,
        summary_path=summary_path,
        expected_review_id="REV-1",
        expected_improvement_id="IMP-1",
        expected_version=1,
        expected_subject_sha256=digest,
    )
    subject = _review_subject()
    projection = {
        "improvement_id": "IMP-1",
        "fingerprint": "f" * 64,
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": "schemas/workflow-schema.yaml",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": "proposed",
        "version": 1,
        "proposed_by_retro_ids": ["RET-1"],
        "last_event_id": "IMPEVT-1",
    }
    structured = {
        "schema_version": "1",
        "review_type": "improvement",
        "decision": "pass",
        "findings": [],
        "evidence_traceability": "complete",
        "scope_readiness": "ready",
        "verification_readiness": "ready",
        "delivery_safety": "ready",
        "human_review_required": False,
    }
    result = AgentRunResult(
        structured_result=cast(JSONValue, structured),
        result_digest=agent_digest(cast(JSONValue, structured)),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    subject_digest = digest_hex(artifact_digest(ImprovementReviewSubject.model_validate(subject)))
    outcome = await execute_task(
        ImprovementReviewFinalizeHandler(),
        cast(
            JSONValue,
            {
                "agent_result": result.model_dump(mode="json"),
                "change_id": _CHANGE_ID,
                "retro_id": "RET-1",
                "owned_evidence_ids": ["PROB-1", "OCC-1"],
                "artifact_paths": [],
                "source_manifest": _review_manifest(),
                "context_digest": _HEX_A,
                "quality_report_digest": _HEX_B,
                "metrics_digest": _HEX_A,
                "issue_digest": _HEX_B,
                "subject": subject,
                "projection": projection,
                "subject_digest": subject_digest,
                "expected_improvement_version": 1,
                "improvement_id": "IMP-1",
                "invocation_id": "inv-1",
                "archive_digest": _HEX_A,
                "locked_signal_ids": ["issue-pattern:PROB-1"],
            },
        ),
        tmp_path / "new",
    )
    assert outcome.status == "succeeded"
    new = as_object(outcome.output)
    assert legacy.decision == new["decision"] == "pass"
    assert legacy.improvement_id == "IMP-1"
    assert legacy.expected_improvement_version == 1
    assert new["review_type"] == "improvement"


def _review_manifest() -> dict[str, object]:
    return {
        "issue_slice_sha256": "a",
        "workflow_slice_sha256": "b",
        "eval_slice_sha256": "c",
        "issue_sources": [
            {
                "kind": "project_problem_ledger",
                "change_id": None,
                "head_event_id": "evt-1",
                "sha256": "abc",
                "evidence_ids": ["PROB-1", "OCC-1"],
            }
        ],
        "workflow_sources": [],
        "eval_sources": [],
    }


def _review_subject() -> dict[str, object]:
    return {
        "schema_version": "1",
        "improvement_id": "IMP-1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "target": "schemas/workflow-schema.yaml",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "signal_evidence": [
            {
                "signal_id": "issue-pattern:PROB-1",
                "summary": "gap",
                "occurrence_count": 3,
                "recommended_change": "register",
                "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
                "confidence": "high",
                "signal_type": "issue_pattern",
                "pattern_kind": "knowledge_gap",
                "affected_surface": {"kind": "knowledge_registry", "value": ".aa/data-knowledge.yaml"},
                "symptom": "missing adapter",
            }
        ],
        "source_manifest": _review_manifest(),
        "pipeline_failures": [],
        "provenance": {
            "retro_id": "RET-1",
            "candidate_id": "C-1",
            "context_sha256": _EVIDENCE,
            "candidate_batch_digest": _EVIDENCE,
        },
    }


async def _compare_signal_complete(tmp_path: Path) -> None:
    from tests.unit.retro.test_signals_validate import _draft, _slice
    from assurance_agent.workflow.retro_outputs import complete_signal_outputs
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest as agent_digest
    from tests.phase4.agent_harness import FakeAgentAdapter

    retro_dir = tmp_path / "qa" / "retro" / "retro-1"
    slice_path = retro_dir / "evidence" / "issue-slice.json"
    signal_path = retro_dir / "signals" / "issue.json"
    slice_path.parent.mkdir(parents=True)
    signal_path.parent.mkdir(parents=True)
    slice_ = _slice()
    slice_path.write_text(json.dumps(slice_.model_dump(mode="json")) + "\n", encoding="utf-8")
    signal_path.write_text(json.dumps(_draft()), encoding="utf-8")
    complete_signal_outputs(tmp_path, ("project:qa/retro/retro-1/signals/issue.json",))
    completed = json.loads(signal_path.read_text(encoding="utf-8"))
    structured = {
        "schema_version": "3",
        "retro_id": "RET-1",
        "domain": None,
        "analysis_status": "ok",
        "failure_reason": None,
        "signals": [
            {
                "signal_id": "issue-pattern:PROB-1",
                "summary": "Existing user/role adapters are absent from L1 capability registry",
                "occurrence_count": 3,
                "recommended_change": "Register the existing adapter and factory symbols",
                "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
                "confidence": "high",
                "signal_type": "issue_pattern",
                "pattern_kind": "knowledge_gap",
                "affected_surface": {"kind": "knowledge_registry", "value": ".aa/data-knowledge.yaml"},
                "symptom": "Existing user/role adapters are absent from L1 capability registry",
            }
        ],
        "candidates": [],
    }
    result = AgentRunResult(
        structured_result=cast(JSONValue, structured),
        result_digest=agent_digest(cast(JSONValue, structured)),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    outcome = await execute_task(
        RetroFinalizeHandler(),
        _retro_finalize_input(result),
        tmp_path / "new",
    )
    assert outcome.status == "succeeded"
    new = as_object(outcome.output)
    assert completed["slice_sha256"].startswith("sha256:")
    assert completed["signals"][0]["source_refs"]["problem_ids"] == ["PROB-1"]
    assert new["signals"][0]["source_refs"]["problem_ids"] == ["PROB-1"]


async def _compare_candidate_complete(tmp_path: Path) -> None:
    from tests.unit.workflow.improvements.test_reconcile_v3 import _candidate, _context
    from assurance_agent.workflow.retro_outputs import complete_candidate_outputs
    from assurance_agent.retro.candidates import context_sha256
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest as agent_digest
    from tests.phase4.agent_harness import FakeAgentAdapter

    context = _context()
    retro_dir = tmp_path / "qa" / "retro" / context.retro_id
    retro_dir.mkdir(parents=True)
    context_bytes = (
        json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (retro_dir / "context.json").write_bytes(context_bytes)
    draft = {
        "schema_version": "3",
        "retro_id": context.retro_id,
        "candidates": [_candidate().model_dump(mode="json")],
    }
    (retro_dir / "proposal-candidates.json").write_text(json.dumps(draft), encoding="utf-8")
    complete_candidate_outputs(
        tmp_path,
        (f"project:qa/retro/{context.retro_id}/proposal-candidates.json",),
    )
    completed = json.loads((retro_dir / "proposal-candidates.json").read_text(encoding="utf-8"))
    structured = {
        "schema_version": "3",
        "retro_id": "RET-1",
        "domain": None,
        "analysis_status": "ok",
        "failure_reason": None,
        "signals": [],
        "candidates": [
            {
                "candidate_id": "C-1",
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
                "target": "schemas/workflow-schema.yaml",
                "rationale": "L1 registry gaps repeatedly block codegen",
                "proposed_change": "Register existing adapter symbols",
                "verification": {"suites": [], "required_cases": [], "success_criteria": "manual review"},
                "risk": "low",
                "confidence": "high",
                "signal_ids": ["issue-pattern:PROB-1"],
            }
        ],
    }
    result = AgentRunResult(
        structured_result=cast(JSONValue, structured),
        result_digest=agent_digest(cast(JSONValue, structured)),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    outcome = await execute_task(
        RetroFinalizeHandler(),
        _retro_finalize_input(result),
        tmp_path / "new",
    )
    assert outcome.status == "succeeded"
    new = as_object(outcome.output)
    assert completed["context_sha256"] == context_sha256(context)
    assert new["candidates"][0]["source_refs"]["problem_ids"] == ["PROB-1"]
    assert completed["candidates"][0]["source_refs"]["problem_ids"] == ["PROB-1"]


def _retro_finalize_input(result: object) -> JSONValue:
    return {
        "agent_result": result.model_dump(mode="json"),  # type: ignore[attr-defined]
        "change_id": _CHANGE_ID,
        "retro_id": "RET-1",
        "owned_evidence_ids": ["PROB-1", "OCC-1"],
        "artifact_paths": [],
        "source_manifest": _review_manifest(),
        "context_digest": _HEX_A,
        "quality_report_digest": _HEX_B,
        "metrics_digest": _HEX_A,
        "issue_digest": _HEX_B,
        "subject_digest": _HEX_A,
        "expected_improvement_version": 1,
        "improvement_id": "IMP-1",
        "invocation_id": "inv-1",
        "archive_digest": _HEX_A,
        "locked_signal_ids": ["issue-pattern:PROB-1"],
    }


def _compare_effect_registrations(_tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import register_healing_effects

    registry = EffectRegistry()
    register_healing_effects(registry)
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    legacy_kinds = frozenset(registry.kinds())
    new_kinds = tuple(sorted(entry.kind for entry in contribution.effects))
    assert legacy_kinds == frozenset(_EFFECT_KIND_MAP)
    assert new_kinds == tuple(sorted(HEALING_EFFECT_IDS))
    assert {_EFFECT_KIND_MAP[kind] for kind in legacy_kinds} == set(HEALING_EFFECT_IDS)


async def _compare_episode(_tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.projection import project_healing_episode_from_events

    events = [
        {
            "type": "healing_attempt_allocated_v2",
            "seq": 1,
            "episode_id": "ep-1",
            "attempt_id": "ha-1",
            "attempt_number": 1,
            "operation_id": "op-1",
            "source_batch_id": "batch-1",
            "entry_batch_id": "batch-1",
            "baseline_sha256": _HEX_A,
            "baseline_embedded": True,
        },
        {
            "type": "fixer_proposal_approved",
            "seq": 2,
            "approval_id": "apr-1",
            "root_invocation_id": "inv-1",
            "interrupt_task_id": "task-1",
            "source_gate_attempt_id": "gate-1",
            "source_tree_id": "tree-src",
            "target_tree_id": "tree-dst",
            "proposal_sha256": _HEX_A,
            "fixer_authority_sha256": _HEX_B,
            "entry_baseline_sha256": _HEX_A,
            "policy_sha256": _HEX_C,
            "targets": ["api"],
            "paths": ["tests/api/test_users.py"],
        },
        {
            "type": "heal_record_apply_v2",
            "seq": 3,
            "record_key": "rec-1",
            "target": "api",
            "outcome": "applied",
            "proposal_ids": ["P1"],
            "claimed_modified_paths": ["tests/api/test_users.py"],
            "intent_sha256": _HEX_A,
            "write_set_id": "ws-1",
            "safety_payload_sha256": _HEX_B,
        },
    ]
    legacy = project_healing_episode_from_events(events)
    outcome = await execute_task(ProjectEpisodeHandler(), cast(JSONValue, {"events": events}))
    output = as_object(outcome.output)
    assert output["episode_id"] == legacy.episode_id == "ep-1"
    assert output["attempts_used"] == legacy.attempts_used
    assert output["approvals"][0]["approval_id"] == legacy.approvals[0].approval_id
    assert output["records"][0]["record_key"] == legacy.records[0].record_key


def _compare_test_tree(tmp_path: Path) -> None:
    from tests.helpers_aa import write_aa_config
    from assurance_agent.workflow.healing.safety import assert_test_tree_unchanged_or_healing

    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / _CHANGE_ID).mkdir(parents=True, exist_ok=True)
    legacy = assert_test_tree_unchanged_or_healing(tmp_path, _CHANGE_ID)
    approved = TestTreeValidator(require_approval=True, approved=True)
    denied = TestTreeValidator(require_approval=True, approved=False)
    context = _validation_context()
    product = approved.validate(_write_set("app/main.py"), context)
    allowed = approved.validate(_write_set("tests/api/test_users.py"), context)
    unapproved = denied.validate(_write_set("tests/api/test_users.py"), context)
    assert legacy.tests_changed is False
    assert product.accepted is False
    assert allowed.accepted is True
    assert unapproved.accepted is False


def _compare_override_allowed(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.override_policy import (
        TestChangesOverridePolicy,
        TestTreeIntegrity,
        assert_test_changes_override_allowed,
    )
    from assurance_agent.exceptions import AaError
    import pytest

    integrity = TestTreeIntegrity(tests_changed=True, changed_files=["tests/api/test_users.py"])
    with pytest.raises(AaError):
        assert_test_changes_override_allowed(
            tmp_path,
            integrity,
            TestChangesOverridePolicy(mode="forbidden"),
        )
    assert_test_changes_override_allowed(
        tmp_path,
        integrity,
        TestChangesOverridePolicy(mode="free", evidence=False),
    )
    token = _override_token()
    accepted = OverrideValidator(
        expected_change_id=_CHANGE_ID,
        policy_digest=_HEX_A,
        candidate_digest=_HEX_B,
        file_bytes={"healing/override-token.json": json.dumps(token).encode()},
    ).validate(_write_set("healing/override-token.json"), _validation_context())
    rejected = OverrideValidator().validate(_write_set("healing/override-token.json"), _validation_context())
    assert accepted.accepted is True
    assert rejected.accepted is False


def _override_token() -> dict[str, object]:
    digest = override_token_digest(
        change_id=_CHANGE_ID,
        policy_digest=_HEX_A,
        candidate_digest=_HEX_B,
    )
    return {
        "schema_version": "1",
        "change_id": _CHANGE_ID,
        "action": "allow_test_changes",
        "reason": "approved",
        "policy_digest": _HEX_A,
        "candidate_digest": _HEX_B,
        "token_digest": digest,
    }


def _compare_build_token(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.override_policy import build_test_changes_override_token

    change_dir = tmp_path / "change"
    change_dir.mkdir()
    legacy = build_test_changes_override_token(
        change_dir,
        change_id=_CHANGE_ID,
        reason="approved",
        tests_tree_sha256=_HEX_A,
        created_at="2026-08-22T00:00:00+00:00",
    )
    current = HealingOverrideTokenV1.model_validate(_override_token())
    assert legacy.change_id == current.change_id == _CHANGE_ID
    assert legacy.action == current.action == "allow_test_changes"


def _compare_policy(_tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.override_policy import load_test_changes_override_policy

    legacy = load_test_changes_override_policy(_tmp_path)
    policy = TestChangePolicyV1.model_validate(
        json.loads(resource_bytes("policy/test-change-policy.v1.json"))
    )
    assert legacy.mode == "with-evidence"
    assert legacy.evidence is True
    assert frozenset(policy.forbidden_product_roots) == _DEFAULT_PRODUCT_ROOTS
    assert policy.require_approval is True


def _compare_token_bytes(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.override_policy import (
        build_test_changes_override_token,
        token_json_bytes,
    )

    change_dir = tmp_path / "change"
    change_dir.mkdir()
    legacy_token = build_test_changes_override_token(
        change_dir,
        change_id=_CHANGE_ID,
        reason="approved",
        tests_tree_sha256=_HEX_A,
        created_at="2026-08-22T00:00:00+00:00",
    )
    raw = token_json_bytes(legacy_token)
    encoded = json.loads(raw)
    current = HealingOverrideTokenV1.model_validate(_override_token())
    assert encoded["change_id"] == current.change_id == _CHANGE_ID
    assert encoded["action"] == current.action == "allow_test_changes"
    assert current.token_digest == override_token_digest(
        change_id=_CHANGE_ID,
        policy_digest=_HEX_A,
        candidate_digest=_HEX_B,
    )


async def _compare_reconcile_allocation(_tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import register_healing_effects

    registry = EffectRegistry()
    register_healing_effects(registry)
    handler = HealingAllocationEffect(store=InMemoryHealingStore())
    intent = _allocation_intent()
    key = str(_allocation_ids()["operation_id"])
    applied = await handler.apply(intent, key)
    reconciled = await handler.reconcile(intent, key)
    assert HEALING_ALLOCATION_V2 in registry.kinds()
    assert applied.status == reconciled.status == "applied"
    assert as_object(reconciled.receipt)["operation_id"] == key


async def _compare_reconcile_approval(_tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import register_healing_effects

    approval_id = derive_approval_id(
        owner_id="assurance.healing",
        candidate_digest=_HEX_C,
        baseline_digest=_HEX_D,
        policy_digest=_HEX_E,
        proposal_digest=_HEX_A,
    )
    payload = {
        "schema_version": "1",
        "approval_id": approval_id,
        "change_id": _CHANGE_ID,
        "owner_id": "assurance.healing",
        "root_invocation_id": "inv-1",
        "interrupt_task_id": "task-1",
        "source_gate_attempt_id": "gate-1",
        "source_tree_id": "tree-src",
        "target_tree_id": "tree-dst",
        "proposal_digest": _HEX_A,
        "fixer_authority_digest": _HEX_B,
        "candidate_digest": _HEX_C,
        "baseline_digest": _HEX_D,
        "policy_digest": _HEX_E,
        "targets": ["api"],
        "paths": ["tests/api/test_users.py"],
        "action": "approve_and_apply",
    }
    registry = EffectRegistry()
    register_healing_effects(registry)
    handler = ProposalApprovedEffect(store=InMemoryHealingStore())
    intent = EffectIntent(
        kind="assurance.healing.effect.proposal-approved.v1",
        payload=cast(JSONValue, payload),
    )
    applied = await handler.apply(intent, approval_id)
    reconciled = await handler.reconcile(intent, approval_id)
    assert FIXER_PROPOSAL_APPROVED_V1 in registry.kinds()
    assert applied.status == reconciled.status == "applied"
    assert as_object(reconciled.receipt)["approval_id"] == approval_id


async def _compare_reconcile_apply(_tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import register_healing_effects

    record_key = derive_heal_record_key(
        owner_id="assurance.healing",
        write_set_id="ws-1",
        candidate_digest=_HEX_A,
        safety_payload_digest=_HEX_E,
        target="api",
    )
    payload = {
        "schema_version": "2",
        "record_key": record_key,
        "change_id": _CHANGE_ID,
        "owner_id": "assurance.healing",
        "target": "api",
        "entry_batch_id": "20260822T000000Z",
        "outcome": "applied",
        "candidate_digest": _HEX_A,
        "baseline_digest": _HEX_B,
        "policy_digest": _HEX_C,
        "write_set_id": "ws-1",
        "proposal_ids": ["P1"],
        "claimed_modified_paths": ["tests/api/test_users.py"],
        "safety_payload_digest": _HEX_E,
    }
    registry = EffectRegistry()
    register_healing_effects(registry)
    handler = HealApplyEffect(store=InMemoryHealingStore())
    intent = EffectIntent(kind="assurance.healing.effect.heal-apply.v2", payload=cast(JSONValue, payload))
    applied = await handler.apply(intent, record_key)
    reconciled = await handler.reconcile(intent, record_key)
    assert HEAL_RECORD_APPLY_V2 in registry.kinds()
    assert applied.status == reconciled.status == "applied"
    assert as_object(reconciled.receipt)["record_key"] == record_key


def _compare_semantic_pins(_tmp_path: Path) -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    pins = next(item for item in ledger.items if item.kind == "hook" and item.legacy_id == "semantic_pins")
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    contributed_ids = {
        *contribution.task_handlers,
        *contribution.commit_validators,
        *(entry.schema_id for entry in contribution.schemas),
        *(entry.resource_id for entry in contribution.resources),
        *(entry.kind for entry in contribution.effects),
    }
    assert pins.new_id is None
    assert pins.owner is None
    assert "semantic_pins" not in contributed_ids
    assert not any("semantic_pin" in item for item in contributed_ids)
    baseline = _phase2_digests()
    mutated = _phase2_digests(mutate_resource=True)
    assert mutated["source_digest"] != baseline["source_digest"]
    assert mutated["composition_digest"] != baseline["composition_digest"]
    assert mutated["lock_digest"] != baseline["lock_digest"]


def _phase2_digests(*, mutate_resource: bool = False) -> dict[str, str]:
    root = REPO_ROOT / "packages" / "assurance-healing"
    files: list[SourceFile] = []
    resource_hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if mutate_resource and rel.endswith("policy/test-change-policy.v1.json"):
            content = content + b"\n"
        files.append(SourceFile.from_bytes(rel, content))
        if "resources/" in rel:
            resource_hashes[rel] = hashlib.sha256(content).hexdigest()
    snapshot = SourceSnapshot.from_files(SourceKind.EDITABLE_PLUGIN, root.resolve(), tuple(files))
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    composition = canonical_digest(
        cast(
            JSONValue,
            {
                "effects": [entry.kind for entry in contribution.effects],
                "handlers": list(contribution.task_handlers),
                "resources": resource_hashes,
                "schemas": {
                    entry.schema_id: hashlib.sha256(bytes(entry.content)).hexdigest()
                    for entry in contribution.schemas
                },
                "validators": list(contribution.commit_validators),
            },
        )
    )
    lock = canonical_digest({"composition_digest": composition, "source_digest": snapshot.digest})
    return {
        "source_digest": snapshot.digest,
        "composition_digest": composition,
        "lock_digest": lock,
    }


def _catchall_scan(roots: Sequence[Path]) -> set[str]:
    hits: set[str] = set()
    for root in roots:
        package = root / root.name.replace("-", "_")
        for path in package.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if node.name in _FORBIDDEN_TYPE_NAMES:
                    hits.add(f"{path.as_posix()}:{node.name}")
                    continue
                if not _is_store_type(node):
                    continue
                kinds = _registry_kinds_in_class(node)
                if len(kinds) > 1:
                    hits.add(f"{path.as_posix()}:{node.name}")
    return hits


def _is_store_type(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        name = _expr_name(decorator)
        if name in {"dataclass", "frozen"}:
            return True
    for base in node.bases:
        if _expr_name(base) in {"Protocol", "TypedDict"}:
            return True
    return False


def _registry_kinds_in_class(node: ast.ClassDef) -> set[str]:
    kinds: set[str] = set()
    for item in node.body:
        annotation = None
        if isinstance(item, ast.AnnAssign):
            annotation = item.annotation
        elif isinstance(item, ast.FunctionDef) and item.name == "__init__":
            for arg in item.args.args[1:]:
                if arg.annotation is not None:
                    kinds.update(_annotation_kinds(arg.annotation))
        if annotation is not None:
            kinds.update(_annotation_kinds(annotation))
    return kinds


def _annotation_kinds(annotation: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(annotation):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in _REGISTRY_KINDS:
            found.add(name)
    return found


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    return None


def _write_set(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id=_HEX_B,
        candidate_tree_id=_HEX_A,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_HEX_A) for path in paths),
    )


def _validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def as_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise TypeError(f"expected mapping, got {type(value)!r}")
