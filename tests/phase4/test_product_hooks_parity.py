from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FunctionType
from typing import Any, cast
from unittest.mock import patch

import yaml
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.contributions import (
    ContributionProjection,
    _resource_projection,
)
from graph_engine.composition.dependencies import resolve_dependency_order
from graph_engine.composition.lock import InvocationLock, build_invocation_lock
from graph_engine.composition.models import (
    AuthenticatedContribution,
    ContributionAuthority,
    ExecutableAuthority,
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    PluginRequirement,
    ProductManifest,
    SourceFile,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceSnapshot,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries
from graph_engine.graph.compiler import compile_workflow
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    EffectIntent,
    PluginContribution,
    PluginDescriptor,
    PluginProvider,
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
from assurance_execution.plugin import ExecutionPlugin
from assurance_generation.plugin import GenerationPlugin
from assurance_healing.plugin import HEALING_EFFECT_IDS, HealingPlugin
from assurance_intake.plugin import IntakePlugin
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
    DurableEffectContext,
    DurableEffectIntentV1,
    DurableEffectRuntime,
    EffectRegistry,
    payload_sha256,
)
from assurance_kernel.workflow.graph.effect_retry import EffectRetryStore, RootEffectFenceStore
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
_PREFIXED_A = f"sha256:{_HEX_A}"
_PREFIXED_B = f"sha256:{_HEX_B}"
_PREFIXED_C = f"sha256:{_HEX_C}"
_PREFIXED_D = f"sha256:{_HEX_D}"
_PREFIXED_E = f"sha256:{_HEX_E}"
_POLICY_RESOURCE = "policy/test-change-policy.v1.json"
_POLICY_RESOURCE_ID = "assurance.healing.policy.test-change-policy.v1"
_PIN_PRODUCT_ID = "toy.assurance"
_PIN_CHAIN: tuple[tuple[PluginProvider, Path], ...] = (
    (IntakePlugin(), REPO_ROOT / "packages" / "assurance-intake"),
    (GenerationPlugin(), REPO_ROOT / "packages" / "assurance-generation"),
    (ExecutionPlugin(), REPO_ROOT / "packages" / "assurance-execution"),
    (HealingPlugin(), REPO_ROOT / "packages" / "assurance-healing"),
)
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

    policy = TestChangePolicyV1.model_validate(
        json.loads(resource_bytes("policy/test-change-policy.v1.json"))
    )
    legacy_canon = frozenset(load_product_code_roots(tmp_path))
    new_canon = frozenset(policy.forbidden_product_roots)
    assert legacy_canon == new_canon


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
    from assurance_agent.workflow.execution.tree_hash import hash_test_tree
    from assurance_agent.workflow.healing.safety import (
        HealingGuardError,
        assert_test_tree_unchanged_or_healing,
    )

    write_aa_config(tmp_path)
    _write_text(tmp_path / "app" / "main.py", "v1\n")
    _write_text(tmp_path / "tests" / "api" / "test_users.py", "def test_ok():\n    assert 1\n")
    change_dir = tmp_path / "qa" / "changes" / _CHANGE_ID
    prior = hash_test_tree(tmp_path)
    _write_text(
        change_dir / "execution" / "execution-manifest.json",
        json.dumps(
            {
                "batch_id": "20260822T000000Z",
                "tests_tree_sha256": prior.aggregate,
                "test_files_sha256": prior.files,
                "product_tree_sha256": "p0",
                "final_status": "PASS",
                "result_files": {},
            }
        ),
    )
    _write_text(tmp_path / "app" / "main.py", "v2\n")
    approved = TestTreeValidator(require_approval=True, approved=True)
    context = _validation_context()
    legacy_product = _legacy_test_tree_allowed(
        tmp_path,
        allow_test_changes=False,
        guard=assert_test_tree_unchanged_or_healing,
        error=HealingGuardError,
    )
    new_product = approved.validate(_write_set("app/main.py"), context).accepted
    _write_text(tmp_path / "tests" / "api" / "test_users.py", "def test_ok():\n    assert 2\n")
    legacy_test = _legacy_test_tree_allowed(
        tmp_path,
        allow_test_changes=True,
        guard=assert_test_tree_unchanged_or_healing,
        error=HealingGuardError,
    )
    new_test = approved.validate(_write_set("tests/api/test_users.py"), context).accepted
    legacy_canon = {"approved_test_accepted": legacy_test, "product_rejected": not legacy_product}
    new_canon = {"approved_test_accepted": new_test, "product_rejected": not new_product}
    assert legacy_canon == new_canon


def _legacy_test_tree_allowed(
    project_root: Path,
    *,
    allow_test_changes: bool,
    guard: object,
    error: type[Exception],
) -> bool:
    try:
        integrity = guard(project_root, _CHANGE_ID, allow_test_changes=allow_test_changes)  # type: ignore[operator]
    except error:
        return False
    return bool(integrity.tests_changed)


def _compare_override_allowed(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.override_policy import (
        TestChangesOverridePolicy,
        TestTreeIntegrity,
        assert_test_changes_override_allowed,
    )
    from assurance_agent.exceptions import AaError

    integrity = TestTreeIntegrity(tests_changed=True, changed_files=["tests/api/test_users.py"])
    legacy_canon = {
        "authorized_accepted": _legacy_override_allowed(
            tmp_path,
            integrity,
            TestChangesOverridePolicy(mode="free", evidence=False),
            guard=assert_test_changes_override_allowed,
            error=AaError,
        ),
        "unauthorized_denied": not _legacy_override_allowed(
            tmp_path,
            integrity,
            TestChangesOverridePolicy(mode="forbidden"),
            guard=assert_test_changes_override_allowed,
            error=AaError,
        ),
    }
    token = _override_token()
    accepted = OverrideValidator(
        expected_change_id=_CHANGE_ID,
        policy_digest=_HEX_A,
        candidate_digest=_HEX_B,
        file_bytes={"healing/override-token.json": json.dumps(token).encode()},
    ).validate(_write_set("healing/override-token.json"), _validation_context())
    rejected = OverrideValidator().validate(_write_set("healing/override-token.json"), _validation_context())
    new_canon = {
        "authorized_accepted": accepted.accepted,
        "unauthorized_denied": not rejected.accepted,
    }
    assert legacy_canon == new_canon


def _legacy_override_allowed(
    change_dir: Path,
    integrity: object,
    policy: object,
    *,
    guard: object,
    error: type[Exception],
) -> bool:
    try:
        guard(change_dir, integrity, policy)  # type: ignore[operator]
    except error:
        return False
    return True


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
    legacy_canon = _shared_token_fields(legacy.model_dump(mode="json"))
    new_canon = _shared_token_fields(current.model_dump(mode="json"))
    assert legacy_canon == new_canon


def _compare_policy(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.override_policy import load_test_changes_override_policy
    from assurance_agent.workflow.healing.safety import load_product_code_roots

    load_test_changes_override_policy(tmp_path)
    policy = TestChangePolicyV1.model_validate(
        json.loads(resource_bytes("policy/test-change-policy.v1.json"))
    )
    legacy_canon = frozenset(load_product_code_roots(tmp_path))
    new_canon = frozenset(policy.forbidden_product_roots)
    assert legacy_canon == new_canon


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
    encoded = json.loads(token_json_bytes(legacy_token))
    current = HealingOverrideTokenV1.model_validate(_override_token())
    new_encoded = json.loads(current.model_dump_json())
    legacy_canon = _shared_token_fields(encoded)
    new_canon = _shared_token_fields(new_encoded)
    assert legacy_canon == new_canon


def _shared_token_fields(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "action": payload["action"],
        "change_id": payload["change_id"],
        "reason": payload["reason"],
    }


async def _compare_reconcile_allocation(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import (
        HealingAllocationEffectV2,
        reconcile_healing_allocation,
    )

    ids = _allocation_ids()
    key = str(ids["operation_id"])
    legacy_payload = HealingAllocationEffectV2(
        schema_version="2",
        episode_id=str(ids["episode_id"]),
        attempt_id=str(ids["attempt_id"]),
        attempt_number=1,
        operation_id=key,
        source_batch_id="batch-src",
        entry_batch_id="batch-src",
        baseline_sha256=_HEX_B,
        baseline_embedded=True,
    ).model_dump(mode="json")
    legacy_ack = reconcile_healing_allocation(
        _legacy_effect_intent(HEALING_ALLOCATION_V2, legacy_payload),
        _durable_context(),
        _durable_runtime(tmp_path),
    )
    handler = HealingAllocationEffect(store=InMemoryHealingStore())
    intent = _allocation_intent()
    applied = await handler.apply(intent, key)
    reconciled = await handler.reconcile(intent, key)
    legacy_canon = {
        "applied": legacy_ack.domain_source_sequence >= 1,
        "operation_id": str(legacy_payload["operation_id"]),
    }
    new_canon = {
        "applied": applied.status == "applied" and reconciled.status == "applied",
        "operation_id": as_object(reconciled.receipt)["operation_id"],
    }
    assert legacy_canon == new_canon


async def _compare_reconcile_approval(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import (
        FixerProposalApprovedEffectV1,
        reconcile_fixer_proposal_approved,
    )

    approval_id = derive_approval_id(
        owner_id="assurance.healing",
        candidate_digest=_HEX_C,
        baseline_digest=_HEX_D,
        policy_digest=_HEX_E,
        proposal_digest=_HEX_A,
    )
    legacy_payload = FixerProposalApprovedEffectV1(
        schema_version="1",
        approval_id=approval_id,
        root_invocation_id="inv-1",
        interrupt_task_id="task-1",
        source_gate_attempt_id="gate-1",
        source_tree_id="tree-src",
        proposal_sha256=_PREFIXED_A,
        fixer_authority_sha256=_PREFIXED_B,
        entry_baseline_sha256=_PREFIXED_D,
        policy_sha256=_PREFIXED_E,
        targets=["api"],
        paths=["tests/api/test_users.py"],
        target_tree_id="tree-dst",
    ).model_dump(mode="json")
    legacy_ack = reconcile_fixer_proposal_approved(
        _legacy_effect_intent(FIXER_PROPOSAL_APPROVED_V1, legacy_payload),
        _durable_context(),
        _durable_runtime(tmp_path),
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
    handler = ProposalApprovedEffect(store=InMemoryHealingStore())
    intent = EffectIntent(
        kind="assurance.healing.effect.proposal-approved.v1",
        payload=cast(JSONValue, payload),
    )
    applied = await handler.apply(intent, approval_id)
    reconciled = await handler.reconcile(intent, approval_id)
    legacy_canon = {
        "applied": legacy_ack.domain_source_sequence >= 1,
        "approval_id": str(legacy_payload["approval_id"]),
    }
    new_canon = {
        "applied": applied.status == "applied" and reconciled.status == "applied",
        "approval_id": as_object(reconciled.receipt)["approval_id"],
    }
    assert legacy_canon == new_canon


async def _compare_reconcile_apply(tmp_path: Path) -> None:
    from assurance_agent.workflow.healing.effects import (
        HealRecordApplyEffectV2,
        reconcile_heal_record_apply,
    )

    record_key = derive_heal_record_key(
        owner_id="assurance.healing",
        write_set_id="ws-1",
        candidate_digest=_HEX_A,
        safety_payload_digest=_HEX_E,
        target="api",
    )
    legacy_payload = HealRecordApplyEffectV2(
        schema_version="2",
        record_key=record_key,
        root_invocation_id="inv-1",
        record_task_id="task-1",
        fixer_attempt_id="att-1",
        target="api",
        entry_batch_id="20260822T000000Z",
        intent_sha256=_PREFIXED_A,
        write_set_id="ws-1",
        outcome="applied",
        proposal_ids=["P1"],
        claimed_modified_paths=["tests/api/test_users.py"],
        safety_payload_sha256=_PREFIXED_E,
    ).model_dump(mode="json")
    legacy_ack = reconcile_heal_record_apply(
        _legacy_effect_intent(HEAL_RECORD_APPLY_V2, legacy_payload),
        _durable_context(),
        _durable_runtime(tmp_path),
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
    handler = HealApplyEffect(store=InMemoryHealingStore())
    intent = EffectIntent(kind="assurance.healing.effect.heal-apply.v2", payload=cast(JSONValue, payload))
    applied = await handler.apply(intent, record_key)
    reconciled = await handler.reconcile(intent, record_key)
    legacy_canon = {
        "applied": legacy_ack.domain_source_sequence >= 1,
        "record_key": str(legacy_payload["record_key"]),
    }
    new_canon = {
        "applied": applied.status == "applied" and reconciled.status == "applied",
        "record_key": as_object(reconciled.receipt)["record_key"],
    }
    assert legacy_canon == new_canon


def _legacy_effect_intent(kind: str, payload: Mapping[str, object]) -> DurableEffectIntentV1:
    wire = {str(key): value for key, value in payload.items()}
    return DurableEffectIntentV1(
        schema_version="1",
        effect_id=_HEX_E,
        kind=kind,
        reconciler_semantics_digest=_HEX_A,
        payload_sha256=payload_sha256(wire),
        payload=wire,
    )


def _durable_context() -> DurableEffectContext:
    return DurableEffectContext(
        root_invocation_id="inv-1",
        invocation_id="inv-1",
        task_id="task-a",
        attempt_id="att-1",
        target="operation:allocate-healing-attempt",
        output_digests={},
    )


def _durable_runtime(tmp_path: Path) -> DurableEffectRuntime:
    change = tmp_path / "change"
    change.mkdir(parents=True, exist_ok=True)
    (change / "events.jsonl").write_text("", encoding="utf-8")
    return DurableEffectRuntime(
        change_dir=change,
        project_root=tmp_path,
        fence_store=RootEffectFenceStore(tmp_path),
        retry_store=EffectRetryStore(tmp_path),
    )


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
    baseline_snapshot = _healing_source_snapshot()
    mutated_snapshot = _healing_source_snapshot(mutate_policy=True)
    assert mutated_snapshot.digest != baseline_snapshot.digest
    baseline = _phase2_digests(source_snapshot=baseline_snapshot, lock_snapshot=baseline_snapshot)
    moved = _phase2_digests(
        source_snapshot=baseline_snapshot,
        lock_snapshot=baseline_snapshot,
        mutate_contribute=True,
    )
    counter = _phase2_digests(source_snapshot=mutated_snapshot, lock_snapshot=baseline_snapshot)
    assert moved["source_digest"] == baseline["source_digest"] == baseline_snapshot.digest
    assert moved["policy_sha256"] != baseline["policy_sha256"]
    assert moved["contribution_digest"] != baseline["contribution_digest"]
    assert moved["composition_digest"] != baseline["composition_digest"]
    assert moved["lock_digest"] != baseline["lock_digest"]
    assert counter["source_digest"] == mutated_snapshot.digest
    assert counter["policy_sha256"] == baseline["policy_sha256"]
    assert counter["contribution_digest"] == baseline["contribution_digest"]
    assert counter["composition_digest"] == baseline["composition_digest"]
    assert counter["lock_digest"] == baseline["lock_digest"]


def _phase2_digests(
    *,
    source_snapshot: SourceSnapshot,
    lock_snapshot: SourceSnapshot,
    mutate_contribute: bool = False,
) -> dict[str, str]:
    contribution = _contributed(mutate_resource=mutate_contribute)
    policy = next(item for item in contribution.resources if item.resource_id == _POLICY_RESOURCE_ID)
    healing = _authenticated_plugin(HealingPlugin.descriptor(), contribution, lock_snapshot)
    projection = ContributionProjection.from_authority(healing.authority)
    lock = _invocation_lock_for_healing(healing, lock_snapshot)
    return {
        "source_digest": source_snapshot.digest,
        "policy_sha256": _resource_projection(policy).content_sha256,
        "contribution_digest": healing.authority.digest,
        "composition_digest": canonical_digest(cast(JSONValue, projection.model_json_projection())),
        "lock_digest": lock.digest,
    }


def _contributed(*, mutate_resource: bool) -> PluginContribution:
    original = resource_bytes

    def patched(relative_path: str) -> bytes:
        raw = original(relative_path)
        if mutate_resource and relative_path == _POLICY_RESOURCE:
            return raw + b"\n"
        return raw

    with patch("assurance_healing.plugin.resource_bytes", patched):
        return HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


def _healing_source_snapshot(*, mutate_policy: bool = False) -> SourceSnapshot:
    root = REPO_ROOT / "packages" / "assurance-healing"
    files: list[SourceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if mutate_policy and rel.endswith(_POLICY_RESOURCE):
            content = content + b"\n"
        files.append(SourceFile.from_bytes(rel, content))
    return _plugin_snapshot(HealingPlugin.descriptor(), root, tuple(files))


def _invocation_lock_for_healing(
    healing: AuthenticatedContribution,
    healing_snapshot: SourceSnapshot,
) -> InvocationLock:
    ports = RegistryPorts(engine_api=ENGINE_API_VERSION)
    authenticated: list[AuthenticatedContribution] = []
    plugin_snapshots: list[SourceSnapshot] = []
    descriptors: dict[str, PluginDescriptor] = {}
    for provider, root in _PIN_CHAIN:
        descriptor = provider.descriptor()
        descriptors[descriptor.plugin_id] = descriptor
        if descriptor.plugin_id == healing.owner_id:
            plugin_snapshots.append(healing_snapshot)
            authenticated.append(healing)
            continue
        snapshot = _plugin_snapshot(descriptor, root, ())
        plugin_snapshots.append(snapshot)
        authenticated.append(_authenticated_plugin(descriptor, provider.contribute(ports), snapshot))
    workflow = _pin_workflow()
    manifest = ProductManifest(
        schema_version="1",
        source=None,
        product_id=_PIN_PRODUCT_ID,
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=tuple(
            PluginRequirement(plugin_id=plugin_id, version_specifier="==0.1.0") for plugin_id in descriptors
        ),
        entrypoints=dict(workflow.entrypoints),
        configuration={},
        workflow=workflow,
    )
    dependency_order = resolve_dependency_order(descriptors, manifest.plugins)
    by_owner = {item.owner_id: item for item in authenticated}
    engine = _engine_snapshot()
    product = _product_snapshot()
    registries = _build_registries(
        sources=(engine, product, *plugin_snapshots),
        contributions=tuple(by_owner[plugin_id] for plugin_id in dependency_order),
        dependency_order=dependency_order,
    )
    return build_invocation_lock(
        manifest=manifest,
        product_snapshot=product,
        descriptors=descriptors,
        dependency_order=dependency_order,
        registries=registries,
        configuration={},
        workflow=compile_workflow(workflow, registries),
        engine_snapshot=engine,
        contribution_authorities={item.owner_id: item.authority for item in authenticated},
    )


def _authenticated_plugin(
    descriptor: PluginDescriptor,
    contribution: PluginContribution,
    snapshot: SourceSnapshot,
) -> AuthenticatedContribution:
    source_key = SourceKey(SourceRole.PLUGIN, descriptor.plugin_id)
    objects = {
        **{
            (ExecutableKind.TASK_HANDLER, registry_id): executable
            for registry_id, executable in contribution.task_handlers.items()
        },
        **{
            (ExecutableKind.COMMIT_VALIDATOR, registry_id): executable
            for registry_id, executable in contribution.commit_validators.items()
        },
        **{
            (kind, registration.kind): registration.handler
            for registration in contribution.effects
            for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
        },
    }
    proofs = tuple(
        sorted(
            (_executable_proof(snapshot, kind, registry_id) for kind, registry_id in objects),
            key=lambda item: (item.registry_id, item.kind.value),
        )
    )
    authority = ContributionAuthority(
        provider_binding=object(),
        descriptor=descriptor,
        owner_id=descriptor.plugin_id,
        source_key=source_key,
        source_digest=snapshot.digest,
        contribution=contribution,
        authorities=tuple(
            ExecutableAuthority(
                executable=objects[(proof.kind, proof.registry_id)],
                function=_slot_function(objects[(proof.kind, proof.registry_id)], proof.kind.slot),
                bound_self=objects[(proof.kind, proof.registry_id)],
                descriptor=_slot_function(objects[(proof.kind, proof.registry_id)], proof.kind.slot),
                provenance=proof,
            )
            for proof in proofs
        ),
    )
    return AuthenticatedContribution(
        owner_id=descriptor.plugin_id,
        source_key=source_key,
        source_digest=snapshot.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=proofs,
        authority=authority,
    )


def _executable_proof(
    snapshot: SourceSnapshot,
    kind: ExecutableKind,
    registry_id: str,
) -> ExecutableProvenance:
    owner_id = snapshot.identity.plugin_id
    if owner_id is None:
        raise AssertionError("plugin snapshot must carry a plugin id")
    return ExecutableProvenance.create(
        kind=kind,
        registry_id=registry_id,
        owner_id=owner_id,
        source_key=SourceKey(SourceRole.PLUGIN, owner_id),
        source_digest=snapshot.digest,
        module=ExecutableModuleProvenance(
            module_name=f"{owner_id.replace('.', '_')}.implementation",
            standard_loader=StandardLoader.SOURCE,
            standard_is_package=False,
            relative_origin="implementation.py",
            authenticated_locations=(),
            physical_sha256="0" * 64,
            source_digest=snapshot.digest,
        ),
        callable_path="implementation:Handler.slot",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )


def _slot_function(executable: object, slot: str) -> FunctionType:
    for cls in type(executable).__mro__:
        found = cls.__dict__.get(slot)
        if isinstance(found, FunctionType):
            return found
    raise AssertionError(f"{type(executable).__name__} has no function slot {slot}")


def _plugin_snapshot(
    descriptor: PluginDescriptor,
    root: Path,
    files: tuple[SourceFile, ...],
) -> SourceSnapshot:
    source = descriptor.source
    if source is None:
        raise AssertionError(f"{descriptor.plugin_id} must declare a wheel source")
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=root.resolve(),
            distribution=source.distribution,
            version=source.version,
            entrypoint_group=source.entrypoint_group,
            entrypoint_name=source.entrypoint_name,
            entrypoint_value=source.entrypoint_value,
            declaration_path=source.declaration_path,
            import_roots=tuple(source.import_roots),
            plugin_id=descriptor.plugin_id,
            plugin_version=descriptor.plugin_version,
        ),
        files,
    )


def _engine_snapshot() -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.ENGINE,
            root=Path("/graph-engine"),
            distribution="graph-engine",
            version="1.0.0",
            engine_installation="installed",
        ),
        (),
    )


def _product_snapshot() -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.PRODUCT_FILE,
            root=Path("/product"),
            product_id=_PIN_PRODUCT_ID,
            product_version="1.0.0",
        ),
        (),
    )


def _pin_workflow() -> WorkflowDef:
    return WorkflowDef.model_validate(
        {
            "name": "pin-proof",
            "entrypoints": {"start": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "done",
                    "nodes": {"done": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )


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


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
