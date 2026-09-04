from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    EffectApplyResult,
    EffectIntent,
    EffectReconcileResult,
    ResourceClaims,
    TaskFailure,
    ValidationContext,
)
from tests.phase4.agent_harness import FakeAgentAdapter

from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.contracts.retro import RetroSourceManifestV3
from assurance_improvement.contracts.review import ImprovementReviewSubject
from assurance_quality.contracts.report import QualityReport

CHANGE_ID = "CH-DEMO-001"
RETRO_ID = "RET-1"
IMPROVEMENT_ID = "IMP-1"
HEX_A = "a" * 64
HEX_B = "b" * 64
EVIDENCE_REF = f"sha256:{HEX_A}"
DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"
PROMOTION_KIND = "assurance.improvement.effect.promotion.v1"
ARCHIVE_KIND = "assurance.improvement.effect.archive.v1"
TARGET_KIND = "memory_apply"
TARGET_DIGEST = HEX_A
PROMOTION_DIGEST = HEX_B
ARCHIVE_DIGEST = HEX_A
INVOCATION_ID = "inv-archive-1"
DELIVERY_KEY = f"{IMPROVEMENT_ID}:1:{TARGET_KIND}:{TARGET_DIGEST}"
PROMOTION_KEY = f"{IMPROVEMENT_ID}:1:{PROMOTION_DIGEST}"
ARCHIVE_KEY = f"{INVOCATION_ID}:{ARCHIVE_DIGEST}"
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
        "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
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
        artifact_paths=["qa/archive/CH-DEMO-001/archive-summary.md"],
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


def delivery_payload(
    *,
    kind: str = "memory_apply",
    improvement_id: str = IMPROVEMENT_ID,
    version: int = 1,
    target_kind: str = TARGET_KIND,
    target_digest: str = TARGET_DIGEST,
    include_receipt: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "kind": kind,
        "improvement_id": improvement_id,
        "version": version,
        "target_kind": target_kind,
        "target_digest": target_digest,
        "target": ".aa/memory/aa-api-plan.md",
    }
    if include_receipt:
        if kind == "memory_eval":
            payload["memory_eval"] = memory_eval_receipt()
        elif kind == "memory_apply":
            payload["memory_apply"] = memory_apply_receipt()
        elif kind == "memory_rollback":
            payload["memory_rollback"] = memory_rollback_receipt()
    return payload


def delivery_intent(**overrides: object) -> EffectIntent:
    payload = delivery_payload(**cast(dict[str, Any], overrides))
    ImprovementEffectIntentV1.model_validate(payload)
    return EffectIntent(kind=DELIVERY_KIND, payload=cast(JSONValue, payload))


def promotion_payload(
    *,
    improvement_id: str = IMPROVEMENT_ID,
    version: int = 1,
    promotion_digest: str = PROMOTION_DIGEST,
    include_receipt: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "kind": "test_promotion",
        "improvement_id": improvement_id,
        "version": version,
        "promotion_digest": promotion_digest,
        "candidate_id": "C-1",
    }
    if include_receipt:
        payload["promotion"] = promotion_receipt_payload(
            improvement_id=improvement_id,
            source_digests={"manifest": promotion_digest},
            write_set=[
                {
                    "path": "tests/api/test_example.py",
                    "before_sha256": None,
                    "after_sha256": promotion_digest,
                }
            ],
        )
    return payload


def promotion_intent(**overrides: object) -> EffectIntent:
    payload = promotion_payload(**cast(dict[str, Any], overrides))
    ImprovementEffectIntentV1.model_validate(payload)
    return EffectIntent(kind=PROMOTION_KIND, payload=cast(JSONValue, payload))


def archive_payload(
    *,
    invocation_id: str = INVOCATION_ID,
    archive_digest: str = ARCHIVE_DIGEST,
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "kind": "archive",
        "improvement_id": IMPROVEMENT_ID,
        "invocation_id": invocation_id,
        "archive_digest": archive_digest,
    }


def archive_intent(**overrides: object) -> EffectIntent:
    payload = archive_payload(**cast(dict[str, Any], overrides))
    ImprovementEffectIntentV1.model_validate(payload)
    return EffectIntent(kind=ARCHIVE_KIND, payload=cast(JSONValue, payload))


class CrashCut(RuntimeError):
    """Simulated crash inside an injected store seam."""


class StoreRecord:
    def __init__(
        self,
        *,
        status: str,
        receipt: dict[str, object] | None = None,
        failure: TaskFailure | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.status = status
        self.receipt = receipt
        self.failure = failure
        self.payload = payload


class InMemoryImprovementStore:
    def __init__(self) -> None:
        self.records: dict[str, StoreRecord] = {}
        self.delivery_count = 0
        self.promotion_count = 0
        self.archive_count = 0

    async def get(self, key: str) -> StoreRecord | None:
        return self.records.get(key)

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
        self.delivery_count += 1
        self.records[key] = StoreRecord(status="applied", receipt=receipt, payload=payload)


class FaultingDeliveryStore:
    def __init__(self, cut: str) -> None:
        self.cut = cut
        self.records: dict[str, StoreRecord] = {}
        self.delivery_count = 0
        self._faulted = False

    async def get(self, key: str) -> StoreRecord | None:
        return self.records.get(key)

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
        if self.cut == "before_mutation" and not self._faulted:
            self._faulted = True
            raise CrashCut("before_mutation")
        if not self._faulted:
            self.delivery_count += 1
            self.records[key] = StoreRecord(status="applied", receipt=receipt, payload=payload)
            self._faulted = True
            if self.cut in {"after_mutation", "before_receipt"}:
                raise CrashCut(self.cut)
            return
        self.delivery_count += 1
        self.records[key] = StoreRecord(status="applied", receipt=receipt, payload=payload)


class FaultingEffectState:
    def __init__(self, cut: str) -> None:
        from graph_engine.effects.state import MemoryEffectState

        self.cut = cut
        self._inner = MemoryEffectState()
        self.delivery_count = 0
        self._faulted = False

    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ):
        if self.cut == "before_mutation" and not self._faulted:
            self._faulted = True
            raise CrashCut("before_mutation")
        result = await self._inner.commit(
            effect_kind=effect_kind,
            settlement_key=settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            payload=payload,
            receipt=receipt,
            fencing_token=fencing_token,
        )
        self.delivery_count += 1
        if not self._faulted and self.cut in {"after_mutation", "before_receipt"}:
            self._faulted = True
            raise CrashCut(self.cut)
        return result

    async def observe(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        fencing_token: int,
    ):
        return await self._inner.observe(
            effect_kind=effect_kind,
            settlement_key=settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            fencing_token=fencing_token,
        )


def _intent_key(intent: EffectIntent) -> str:
    payload = intent.payload
    if not isinstance(payload, dict):
        raise TypeError("effect payload must be a mapping")
    if intent.kind == ARCHIVE_KIND:
        return f"{payload['invocation_id']}:{payload['archive_digest']}"
    if intent.kind == PROMOTION_KIND:
        return f"{payload['improvement_id']}:{payload['version']}:{payload['promotion_digest']}"
    return (
        f"{payload['improvement_id']}:{payload['version']}:"
        f"{payload['target_kind']}:{payload['target_digest']}"
    )


async def _attempt_and_reconcile(
    handler: object, intent: EffectIntent, context: object
) -> EffectReconcileResult:
    apply = getattr(handler, "apply")
    reconcile = getattr(handler, "reconcile")
    try:
        applied = await apply(intent, context)
        if isinstance(applied, EffectApplyResult) and applied.status == "applied":
            result = await reconcile(intent, context)
            if isinstance(result, EffectReconcileResult):
                return result
    except CrashCut:
        pass
    reconciled = await reconcile(intent, context)
    if not isinstance(reconciled, EffectReconcileResult):
        raise TypeError("reconcile must return EffectReconcileResult")
    if reconciled.status == "not_applied":
        try:
            applied = await apply(intent, context)
        except CrashCut:
            applied = None
        if not isinstance(applied, EffectApplyResult) or applied.status != "applied":
            return EffectReconcileResult(status="pending")
        retry = await reconcile(intent, context)
        if not isinstance(retry, EffectReconcileResult):
            raise TypeError("reconcile must return EffectReconcileResult")
        return retry
    return reconciled


def write_set(*paths: str, digest: str = HEX_A) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id=HEX_B,
        candidate_tree_id=HEX_A,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=digest) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
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
