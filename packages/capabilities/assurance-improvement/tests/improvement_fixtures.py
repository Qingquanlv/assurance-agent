from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
)
from tests.capabilities.agent_harness import FakeAgentAdapter

from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.contracts.retro import RetroSourceManifestV3
from assurance_improvement.contracts.review import ImprovementReviewSubject
from assurance_quality.contracts.report import QualityReport

CHANGE_ID = "CH-DEMO-001"
RETRO_ID = "RET-1"
IMPROVEMENT_ID = "IMP-1"
HEX_A = "a" * 64
HEX_B = "b" * 64
EVIDENCE_REF = f"sha256:{HEX_A}"
PROMOTION_DIGEST = HEX_B
ARCHIVE_DIGEST = HEX_A
INVOCATION_ID = "inv-archive-1"
_WHEEL_ROOT = Path(__file__).resolve().parent.parent

REFS = {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]}
BINDING: dict[str, JSONValue] = {
    "agent_profile": "aa-doc-author",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": HEX_A,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": HEX_A,
    "request_config_digest": HEX_A,
}


def as_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise TypeError(f"expected mapping, got {type(value)!r}")


def authenticated_manifest() -> dict[str, object]:
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


def retro_manifest() -> RetroSourceManifestV3:
    return RetroSourceManifestV3.model_validate(authenticated_manifest())


def candidate_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_id": "C-1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": REFS,
        "target": "schemas/workflow-schema.yaml",
        "rationale": "L1 registry gaps repeatedly block codegen",
        "proposed_change": "Register existing adapter symbols",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "manual review"},
        "risk": "low",
        "confidence": "high",
        "signal_ids": ["issue-pattern:l1_knowledge_missing_symbol"],
    }
    payload.update(overrides)
    return payload


def issue_signal(*, source_id: str = "PROB-1") -> dict[str, object]:
    refs = {"problem_ids": [source_id], "occurrence_ids": ["OCC-1"] if source_id == "PROB-1" else []}
    if source_id != "PROB-1" and source_id.startswith("OCC-"):
        refs = {"occurrence_ids": [source_id]}
    return {
        "signal_id": f"issue-pattern:{source_id}",
        "summary": "Existing user/role adapters are absent from L1 capability registry",
        "occurrence_count": 3,
        "recommended_change": "Register the existing adapter and factory symbols",
        "source_refs": refs,
        "confidence": "high",
        "signal_type": "issue_pattern",
        "pattern_kind": "knowledge_gap",
        "affected_surface": {"kind": "knowledge_registry", "value": ".aa/data-knowledge.yaml"},
        "symptom": "Existing user/role adapters are absent from L1 capability registry",
    }


def retro_result(*, source_id: str = "PROB-1", domain: str | None = None) -> JSONValue:
    return cast(
        JSONValue,
        {
            "schema_version": "3",
            "retro_id": RETRO_ID,
            "domain": domain,
            "analysis_status": "ok",
            "failure_reason": None,
            "signals": [issue_signal(source_id=source_id)],
            "candidates": [
                candidate_payload(
                    source_refs={"problem_ids": [source_id], "occurrence_ids": []},
                    signal_ids=[f"issue-pattern:{source_id}"],
                ),
            ],
        },
    )


def fake_agent_result(structured_result: JSONValue, **locks: JSONValue) -> JSONValue:
    result = AgentRunResult(
        result_payload=structured_result,
        result_digest=canonical_digest(structured_result),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    payload: dict[str, JSONValue] = {
        "agent_result": cast(JSONValue, result.model_dump(mode="json")),
        "change_id": CHANGE_ID,
        "retro_id": RETRO_ID,
        "owned_evidence_ids": ["PROB-1", "OCC-1"],
        "artifact_paths": [],
        "source_manifest": cast(JSONValue, authenticated_manifest()),
        "context_digest": HEX_A,
        "quality_report_digest": HEX_B,
        "metrics_digest": HEX_A,
        "issue_digest": HEX_B,
        "subject_digest": HEX_A,
        "expected_improvement_version": 1,
        "improvement_id": IMPROVEMENT_ID,
        "invocation_id": INVOCATION_ID,
        "archive_digest": ARCHIVE_DIGEST,
        "locked_signal_ids": ["issue-pattern:PROB-1"],
    }
    payload.update(locks)
    return payload


def archive_result(**overrides: object) -> dict[str, object]:
    risk = overrides.get("issue_risk", "clear")
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "archive_status": "archived",
        "issue_risk": "clear",
        "issue_risk_rationale": "no active issues" if risk in {None, "clear"} else "1 active issue",
        "summary": f"# Archive {CHANGE_ID}\n",
        "artifact_paths": ["qa/results/archive-summary.md"],
        "invocation_id": INVOCATION_ID,
        "archive_digest": ARCHIVE_DIGEST,
    }
    payload.update(overrides)
    return payload


def improvement_projection(
    *,
    state: str = "approved",
    delivery: str = "memory_patch",
    version: int = 1,
    improvement_id: str = IMPROVEMENT_ID,
    kind: str | None = None,
    target: str | None = None,
) -> dict[str, object]:
    resolved_kind = kind or (
        "prompt_improvement"
        if delivery == "memory_patch"
        else "domain_knowledge"
        if delivery == "knowledge_delta"
        else "workflow_improvement"
    )
    resolved_target = target or (
        ".aa/memory/aa-api-plan.md" if delivery == "memory_patch" else "schemas/workflow-schema.yaml"
    )
    return {
        "improvement_id": improvement_id,
        "fingerprint": "f" * 64,
        "kind": resolved_kind,
        "delivery": delivery,
        "source_refs": REFS,
        "target": resolved_target,
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": state,
        "version": version,
        "proposed_by_retro_ids": [RETRO_ID],
        "last_event_id": "IMPEVT-1",
        "approval_source": "automatic" if state == "approved" else "none",
        "last_auto_review": {
            "review_id": "REV-1",
            "subject_sha256": EVIDENCE_REF,
            "assessment_sha256": EVIDENCE_REF,
            "policy_version": "1",
            "verdict": "auto_approved",
        }
        if state == "approved"
        else None,
    }


def review_subject(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "improvement_id": IMPROVEMENT_ID,
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "target": "schemas/workflow-schema.yaml",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "source_refs": REFS,
        "signal_evidence": [issue_signal()],
        "source_manifest": authenticated_manifest(),
        "pipeline_failures": [],
        "provenance": {
            "retro_id": RETRO_ID,
            "candidate_id": "C-1",
            "context_sha256": EVIDENCE_REF,
            "candidate_batch_digest": EVIDENCE_REF,
        },
    }
    payload.update(overrides)
    return payload


def locked_review_input(structured_result: JSONValue, **locks: JSONValue) -> JSONValue:
    subject = review_subject()
    projection = improvement_projection(state="proposed", delivery="change_draft")
    digest = digest_hex(artifact_digest(ImprovementReviewSubject.model_validate(subject)))
    payload = fake_agent_result(
        structured_result,
        subject=cast(JSONValue, subject),
        projection=cast(JSONValue, projection),
        subject_digest=digest,
        expected_improvement_version=1,
        improvement_id=IMPROVEMENT_ID,
    )
    if isinstance(payload, dict):
        payload.update(locks)
    return payload


def locked_archive_input(structured_result: JSONValue, **locks: JSONValue) -> JSONValue:
    report = quality_report_payload(issue_risk=str(as_object(structured_result).get("issue_risk") or "clear"))
    digest = digest_hex(artifact_digest(QualityReport.model_validate(report)))
    payload = fake_agent_result(
        structured_result,
        quality_report=cast(JSONValue, report),
        quality_report_digest=digest,
        artifact_paths=["qa/results/archive-summary.md"],
    )
    if isinstance(payload, dict):
        payload.update(locks)
    return payload


def memory_eval_receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
    }
    payload.update(overrides)
    return payload


def memory_apply_receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "target": ".aa/memory/aa-api-plan.md",
        "before_sha256": "b",
        "after_sha256": "a",
        "receipt_sha256": "r",
    }
    payload.update(overrides)
    return payload


def memory_rollback_receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "target": ".aa/memory/aa-api-plan.md",
        "restored_sha256": "x",
        "reason": "regressed",
    }
    payload.update(overrides)
    return payload


def promotion_receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "receipt_id": "promo-1",
        "improvement_id": IMPROVEMENT_ID,
        "candidate_id": "C-1",
        "applied_at": "2026-08-21T12:00:00Z",
        "write_set": [
            {
                "path": "tests/api/test_example.py",
                "before_sha256": None,
                "after_sha256": PROMOTION_DIGEST,
            }
        ],
        "status": "applied",
        "source_digests": {"manifest": PROMOTION_DIGEST},
        "write_authorization": ["tests/api/test_example.py"],
    }
    payload.update(overrides)
    return payload


def quality_report_payload(*, issue_risk: str = "clear") -> dict[str, object]:
    counts = {"total": 1, "passed": 1, "failed": 0}
    return {
        "schema_version": "1.1",
        "change_id": CHANGE_ID,
        "batch_id": "20260822T000000Z",
        "plan": {
            "plan_digest": HEX_A,
            "plan_ref": {
                "path": f"qa/results/plan/{HEX_A}/resolved-assurance-plan.json",
                "digest": HEX_B,
            },
        },
        "final_status": "PASS",
        "quality_score": 1.0,
        "score_breakdown": {"functional": 1.0, "coverage": 1.0, "fuzz": "N/A", "performance": "N/A"},
        "scope": {"cases": 1, "requirements": []},
        "functional": {"status": "PASS", "api": counts, "e2e": counts},
        "coverage": {
            "status": "PASS",
            "available": True,
            "line_coverage": 1.0,
            "branch_coverage": 1.0,
            "threshold": {"line": 0.8, "branch": 0.8},
        },
        "defects": {"product": [], "test": [], "environment": []},
        "risk_level": "LOW",
        "risk_rationale": "ok",
        "recommendation": "ship",
        "issues": {
            "analysis_status": "completed",
            "project_sync_status": "synced",
            "total_occurrences": 1,
            "counts_by_status": {},
            "counts_by_classification": {},
            "counts_by_severity": {},
            "new_count": 0,
            "repeated_count": 0,
            "regressed_count": 0,
            "resolved_count": 0,
            "accepted_risk_count": 0,
            "not_an_issue_count": 0,
            "issue_risk": issue_risk,
            "issue_risk_rationale": "1 active issue" if issue_risk != "clear" else "no active issues",
        },
    }


def skill_input() -> JSONValue:
    return cast(
        JSONValue,
        {
            "change_id": CHANGE_ID,
            "retro_id": RETRO_ID,
            "owned_evidence_ids": ["PROB-1", "OCC-1"],
            "artifact_paths": ["retro/context.json"],
            "source_manifest": authenticated_manifest(),
            "context_digest": HEX_A,
            "quality_report_digest": HEX_B,
            "metrics_digest": HEX_A,
            "issue_digest": HEX_B,
            "subject_digest": HEX_A,
            "expected_improvement_version": 1,
            "improvement_id": IMPROVEMENT_ID,
            "invocation_id": INVOCATION_ID,
            "archive_digest": ARCHIVE_DIGEST,
            "locked_signal_ids": ["issue-pattern:PROB-1"],
        },
    )


def json_value(payload: dict[str, Any]) -> JSONValue:
    return cast(JSONValue, payload)


def write_set(*paths: str, digest: str = HEX_A) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id=HEX_B,
        candidate_tree_id=HEX_A,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=digest) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="capabilities-test",
        task_id="capabilities-task",
        graph_instance_id="capabilities-graph",
        node_id="capabilities-node",
        resources=ResourceClaims(),
    )


def imported_module_names(root: Path) -> set[str]:
    names: set[str] = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


def production_import_roots() -> set[str]:
    return imported_module_names(_WHEEL_ROOT / "assurance_improvement")


def imported_symbols(package: str) -> set[str]:
    root = _WHEEL_ROOT / package
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[-1] for alias in node.names)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
                if node.module:
                    names.add(node.module)
                    names.update(node.module.split("."))
    return names
