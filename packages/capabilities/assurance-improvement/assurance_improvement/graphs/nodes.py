from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.plugin_api import FrozenModel

from assurance_improvement.contracts.agent import ImprovementSkillInputV1
from assurance_improvement.contracts.attempts import (
    select_analysis_slice,
    select_evaluate_memory,
    select_retro_agent,
    select_retro_collect,
    select_retro_reconcile,
)
from assurance_improvement.contracts.delivery import artifact_digest, same_digest
from assurance_improvement.contracts.improvements import ImprovementLedgerProjection
from assurance_improvement.contracts.retro import (
    ContextSignalSet,
    DomainAnalysisStatus,
    DomainStatuses,
    RetroContextV3,
    RetroIntegrity,
    RetroSourceManifestV3,
    SignalDocumentV3,
)

APPLY_HUMAN_ACTIONS = ("approve", "reject", "request_rework", "supersede")
EFFECT_IDS = frozenset(
    {
        "assurance.improvement.effect.archive.v1",
        "assurance.improvement.effect.delivery.v1",
        "assurance.improvement.effect.promotion.v1",
    }
)
activation_one_shot = BusinessActivation.one_shot()
_SKILL_FIELDS = (
    "change_id",
    "retro_id",
    "owned_evidence_ids",
    "source_manifest",
    "context_digest",
    "quality_report_digest",
    "metrics_digest",
    "issue_digest",
    "subject_digest",
    "expected_improvement_version",
    "improvement_id",
    "invocation_id",
    "archive_digest",
    "locked_signal_ids",
)


class ApplyHumanDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework", "supersede"]


def _output_payload(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def _receipt_payload(receipt: object) -> Mapping[str, object]:
    if isinstance(receipt, BaseModel):
        return receipt.model_dump(mode="json")
    if isinstance(receipt, Mapping):
        return receipt
    return {}


def _receipt_effect_refs(receipt: object) -> list[dict[str, str]]:
    raw = _receipt_payload(receipt).get("effect_refs") or _receipt_payload(receipt).get("receipt_refs") or []
    if not isinstance(raw, list):
        return []
    refs: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        digest = item.get("digest")
        if isinstance(kind, str) and kind in EFFECT_IDS and isinstance(digest, str):
            refs.append({"kind": kind, "digest": digest})
    return refs


def _pick(state: Mapping[str, object], *names: str) -> dict[str, object]:
    return {name: state[name] for name in names if name in state}


def select_skill(state: Mapping[str, object]) -> ImprovementSkillInputV1:
    artifact_paths = state.get("artifact_paths") or state.get("allowed_artifact_paths") or ()
    return ImprovementSkillInputV1.model_validate(
        {
            **{name: state[name] for name in _SKILL_FIELDS},
            "artifact_paths": artifact_paths,
        }
    )


def select_collect(state: Mapping[str, object]) -> object:
    return select_retro_collect(
        _pick(
            state,
            "retro_id",
            "window",
            "issue_slice",
            "workflow_slice",
            "eval_slice",
            "discovery_slice",
            "coverage_gap_slice",
        )
    )


def publish_collect(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    collected = select_retro_collect(_output_payload(output))
    return collected.model_dump(mode="json")


def _select_analysis(
    state: Mapping[str, object], domain: Literal["issue", "workflow", "eval"]
) -> ImprovementSkillInputV1:
    collected = select_retro_collect(
        _pick(
            state,
            "retro_id",
            "window",
            "issue_slice",
            "workflow_slice",
            "eval_slice",
            "discovery_slice",
            "coverage_gap_slice",
        )
    )
    select_analysis_slice(collected, domain=domain)
    return select_skill(state)


def select_eval_analysis(state: Mapping[str, object]) -> ImprovementSkillInputV1:
    return _select_analysis(state, "eval")


def select_issue_analysis(state: Mapping[str, object]) -> ImprovementSkillInputV1:
    return _select_analysis(state, "issue")


def select_workflow_analysis(state: Mapping[str, object]) -> ImprovementSkillInputV1:
    return _select_analysis(state, "workflow")


def publish_eval_analysis(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    return {"eval_analysis": _output_payload(output)}


def publish_issue_analysis(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    return {"issue_analysis": _output_payload(output)}


def publish_workflow_analysis(
    state: Mapping[str, object], output: object, receipt: object
) -> dict[str, object]:
    del state, receipt
    return {"workflow_analysis": _output_payload(output)}


def _signal_document(
    analysis: Mapping[str, object],
    *,
    domain: str,
    retro_id: str,
    slice_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": analysis.get("schema_version") or "3",
        "retro_id": analysis.get("retro_id") or retro_id,
        "domain": analysis.get("domain") or domain,
        "analysis_status": analysis.get("analysis_status") or "ok",
        "failure_reason": analysis.get("failure_reason"),
        "analyzer": analysis.get("analyzer") or f"aa-retro-{domain}-analysis",
        "signals": analysis.get("signals") or (),
        "slice_sha256": analysis.get("slice_sha256") or slice_sha256,
    }


def assemble_analyses(state: Mapping[str, object]) -> dict[str, object]:
    eval_analysis = state.get("eval_analysis")
    issue_analysis = state.get("issue_analysis")
    workflow_analysis = state.get("workflow_analysis")
    if not isinstance(eval_analysis, Mapping):
        raise ValueError("eval analysis result is required")
    if not isinstance(issue_analysis, Mapping):
        raise ValueError("issue analysis result is required")
    if not isinstance(workflow_analysis, Mapping):
        raise ValueError("workflow analysis result is required")
    collected = select_retro_collect(
        _pick(
            state,
            "retro_id",
            "window",
            "issue_slice",
            "workflow_slice",
            "eval_slice",
            "discovery_slice",
            "coverage_gap_slice",
        )
    )
    issue_slice = select_analysis_slice(collected, domain="issue")
    workflow_slice = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    issue_digest = artifact_digest(issue_slice)
    workflow_digest = artifact_digest(workflow_slice)
    eval_digest = artifact_digest(evaluation)
    domains = (
        ("issue", issue_slice, issue_analysis, issue_digest),
        ("workflow", workflow_slice, workflow_analysis, workflow_digest),
        ("eval", evaluation, eval_analysis, eval_digest),
    )
    statuses: dict[str, DomainAnalysisStatus] = {}
    signals: dict[str, tuple[Any, ...]] = {}
    reasons: list[str] = []
    for domain, slice_, analysis, digest in domains:
        signal_doc = SignalDocumentV3.model_validate(
            _signal_document(
                analysis,
                domain=domain,
                retro_id=collected.retro_id,
                slice_sha256=digest,
            )
        )
        if slice_.window != collected.window or slice_.retro_id != signal_doc.retro_id:
            raise ValueError(f"{domain} assembly identity mismatch")
        actual = artifact_digest(slice_)
        if not same_digest(actual, digest) or not same_digest(actual, signal_doc.slice_sha256):
            raise ValueError(f"{domain} slice digest is not authenticated")
        if signal_doc.analysis_status == "failed":
            statuses[domain] = DomainAnalysisStatus(status="failed", failure_reason=signal_doc.failure_reason)
            signals[domain] = slice_.deterministic_signals
            reasons.append(f"{domain}_signal_analysis_failed")
        else:
            statuses[domain] = DomainAnalysisStatus(status="ok")
            merged: dict[str, object] = {signal.signal_id: signal for signal in slice_.deterministic_signals}
            for signal in signal_doc.signals:
                merged[signal.signal_id] = signal
            signals[domain] = tuple(merged.values())
        for reason in slice_.integrity.reasons:
            if reason not in reasons:
                reasons.append(reason)
    context = RetroContextV3(
        retro_id=collected.retro_id,
        generated_at=str(state["ts"]),
        dry_run=bool(state.get("dry_run") or False),
        window=collected.window,
        source_manifest=RetroSourceManifestV3(
            issue_slice_sha256=issue_digest,
            workflow_slice_sha256=workflow_digest,
            eval_slice_sha256=eval_digest,
            issue_sources=issue_slice.sources,
            workflow_sources=workflow_slice.sources,
            eval_sources=evaluation.sources,
        ),
        integrity=(
            RetroIntegrity(status="incomplete", reasons=tuple(dict.fromkeys(reasons)))
            if reasons
            else RetroIntegrity(status="complete")
        ),
        domain_status=DomainStatuses(
            issue=statuses["issue"],
            workflow=statuses["workflow"],
            eval=statuses["eval"],
        ),
        signals=ContextSignalSet.model_validate(
            {"issue": signals["issue"], "workflow": signals["workflow"], "eval": signals["eval"]}
        ),
        signal_count=sum(len(items) for items in signals.values()),
    )
    candidates: list[object] = []
    for payload in (issue_analysis, workflow_analysis, eval_analysis):
        items = payload.get("candidates") or ()
        if isinstance(items, list):
            candidates.extend(items)
    return {"context": context.model_dump(mode="json"), "candidates": candidates}


def select_reconcile(state: Mapping[str, object]):
    raw_candidates = state.get("candidates") or ()
    if not isinstance(raw_candidates, (list, tuple)):
        raw_candidates = ()
    return select_retro_reconcile(
        context=state["context"],
        candidates=tuple(raw_candidates),
        current=state["current"],
        ts=str(state["ts"]),
    )


def publish_reconcile(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    ledger = _output_payload(output)
    return {"ledger": ledger}


def select_retro(state: Mapping[str, object]) -> ImprovementSkillInputV1:
    raw = state.get("ledger")
    if raw is None:
        raise ValueError("retro Agent requires the reconciled typed ledger")
    ledger = ImprovementLedgerProjection.model_validate(raw)
    select_retro_agent(ledger)
    return select_skill(state)


def publish_retro(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    return {
        "change_id": state.get("change_id"),
        "retro_id": payload.get("retro_id", state.get("retro_id")),
        "lifecycle_state": state.get("lifecycle_state"),
        "evidence_refs": state.get("evidence_refs") or [],
    }


def select_evaluate(state: Mapping[str, object]) -> object:
    return select_evaluate_memory(
        _pick(
            state,
            "projection",
            "eval_run_id",
            "outcome",
            "report_sha256",
            "staged_sha256",
            "baseline_sha256",
            "target_digest",
        )
    )


def publish_evaluate(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state
    payload = _output_payload(output)
    refs = _receipt_effect_refs(receipt)
    return {
        "eval_run_id": payload["eval_run_id"],
        "outcome": payload["outcome"],
        "report_sha256": payload["report_sha256"],
        "staged_sha256": payload["staged_sha256"],
        "baseline_sha256": payload.get("baseline_sha256"),
        "approved_state_digest": payload.get("approved_state_digest"),
        "approved_version": payload.get("approved_version"),
        "memory_eval": payload,
        "effect_refs": refs,
        "receipt_refs": refs,
    }


def select_auto_review(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "assessment": state["assessment"],
        "current": state.get("current") or state.get("projection"),
    }


def publish_auto_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    projection = payload.get("projection")
    lifecycle = payload.get("lifecycle_state")
    if lifecycle is None and isinstance(projection, Mapping):
        lifecycle = projection.get("state")
    return {
        "lifecycle_state": lifecycle or state.get("lifecycle_state"),
        "projection": projection or state.get("projection"),
        "current": projection or state.get("current"),
    }


def select_human_review(state: Mapping[str, object]) -> dict[str, object]:
    projection = state.get("projection") or state.get("current")
    expected = state.get("expected_improvement_version")
    if expected is None and isinstance(projection, Mapping):
        expected = projection.get("version")
    return {
        "projection": projection,
        "action": state["human_action"],
        "review_id": state["review_id"],
        "expected_improvement_version": expected,
    }


def publish_human_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    lifecycle = payload.get("state") or payload.get("lifecycle_state")
    return {
        "lifecycle_state": lifecycle or state.get("lifecycle_state"),
        "projection": payload,
        "current": payload,
    }


def select_export(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "projection": state["projection"],
        "artifact_path": state["artifact_path"],
        "sha256": state["sha256"],
        "created": state["created"],
        "target_digest": state["target_digest"],
    }


def publish_export(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state
    payload = _output_payload(output)
    refs = _receipt_effect_refs(receipt)
    return {
        "artifact_path": payload["artifact_path"],
        "sha256": payload["sha256"],
        "created": payload["created"],
        "effect_refs": refs,
        "receipt_refs": refs,
    }


def select_apply_memory(state: Mapping[str, object]) -> dict[str, object]:
    eval_receipt = state.get("memory_eval")
    if eval_receipt is None:
        eval_receipt = {
            name: state[name]
            for name in (
                "eval_run_id",
                "outcome",
                "report_sha256",
                "staged_sha256",
                "baseline_sha256",
                "approved_state_digest",
                "approved_version",
            )
            if name in state
        }
    return {
        "projection": state.get("projection") or state.get("current"),
        "eval_receipt": eval_receipt,
        "approved_state_digest": state["approved_state_digest"],
        "approved_version": state["approved_version"],
        "before_sha256": state["before_sha256"],
        "after_sha256": state["after_sha256"],
        "receipt_sha256": state["receipt_sha256"],
        "target_digest": state["target_digest"],
    }


def publish_apply_memory(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state
    payload = _output_payload(output)
    refs = _receipt_effect_refs(receipt)
    return {
        "target": payload.get("target"),
        "before_sha256": payload.get("before_sha256"),
        "after_sha256": payload.get("after_sha256"),
        "receipt_sha256": payload.get("receipt_sha256"),
        "outcome": "passed",
        "lifecycle_state": "applied",
        "effect_refs": refs,
        "receipt_refs": refs,
    }


def select_rollback(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "projection": state["projection"],
        "reason": state["reason"],
        "restored_sha256": state["restored_sha256"],
        "target_digest": state["target_digest"],
    }


def publish_rollback(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state
    payload = _output_payload(output)
    refs = _receipt_effect_refs(receipt)
    return {
        "target": payload.get("target"),
        "restored_sha256": payload.get("restored_sha256"),
        "reason": payload.get("reason"),
        "lifecycle_state": "rolled_back",
        "effect_refs": refs,
        "receipt_refs": refs,
    }


def publish_archive(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    payload = _output_payload(output)
    refs = _receipt_effect_refs(receipt)
    return {
        "change_id": payload.get("change_id", state.get("change_id")),
        "archive_status": payload.get("archive_status"),
        "lifecycle_state": state.get("lifecycle_state"),
        "evidence_refs": state.get("evidence_refs") or [],
        "effect_refs": refs,
        "receipt_refs": refs,
    }


def publish_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    return {
        "change_id": state.get("change_id"),
        "decision": payload.get("decision"),
        "human_review_required": payload.get("human_review_required"),
        "lifecycle_state": state.get("lifecycle_state"),
        "evidence_refs": state.get("evidence_refs") or [],
    }


def _coerce_human_decision(raw: object) -> ApplyHumanDecision:
    if isinstance(raw, str):
        return ApplyHumanDecision(action=raw)  # type: ignore[arg-type]
    if isinstance(raw, Mapping):
        action = raw.get("action", raw.get("decision"))
        return ApplyHumanDecision.model_validate({"action": action})
    return ApplyHumanDecision.model_validate(raw)


def apply_human_interrupt(state: Mapping[str, object]) -> dict[str, object]:
    del state
    raw = interrupt(
        {
            "reason": "needs_human_review",
            "actions": list(APPLY_HUMAN_ACTIONS),
            "interrupt_id": "improvement-apply-human-review",
            "ordinal": 0,
        }
    )
    decision = _coerce_human_decision(raw)
    return {"human_action": decision.action}


def _public_terminal(state: Mapping[str, object], status: str) -> dict[str, object]:
    lifecycle = state.get("lifecycle_state")
    if status == "failed" and lifecycle not in {
        "approved",
        "needs_rework",
        "rejected",
        "superseded",
        "proposed",
        "applied",
        "rolled_back",
    }:
        lifecycle = "failed"
    return {
        "change_id": state.get("change_id"),
        "lifecycle_state": lifecycle or status,
        "status": status,
        "outcome": state.get("outcome"),
        "decision": state.get("decision"),
        "archive_status": state.get("archive_status"),
        "eval_analysis": state.get("eval_analysis"),
        "issue_analysis": state.get("issue_analysis"),
        "workflow_analysis": state.get("workflow_analysis"),
        "receipt_refs": state.get("receipt_refs") or [],
        "effect_refs": state.get("effect_refs") or [],
    }


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "done")


def terminal_failed(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "failed")


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "rejected")


def terminal_rework(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "rework")


def terminal_superseded(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "superseded")


__all__ = [
    "APPLY_HUMAN_ACTIONS",
    "ApplyHumanDecision",
    "activation_one_shot",
    "apply_human_interrupt",
    "assemble_analyses",
    "publish_apply_memory",
    "publish_archive",
    "publish_auto_review",
    "publish_collect",
    "publish_eval_analysis",
    "publish_evaluate",
    "publish_export",
    "publish_human_review",
    "publish_issue_analysis",
    "publish_reconcile",
    "publish_review",
    "publish_rollback",
    "publish_retro",
    "publish_workflow_analysis",
    "select_apply_memory",
    "select_auto_review",
    "select_collect",
    "select_eval_analysis",
    "select_evaluate",
    "select_export",
    "select_human_review",
    "select_issue_analysis",
    "select_reconcile",
    "select_retro",
    "select_rollback",
    "select_skill",
    "select_workflow_analysis",
    "terminal_done",
    "terminal_failed",
    "terminal_rejected",
    "terminal_rework",
    "terminal_superseded",
]
