from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.plugin_api import FrozenModel

from assurance_improvement.contracts.agent import (
    ImprovementSkillInputV1,
    RetroAnalysisInputV1,
    RetroSynthesisInputV1,
    RetroAnalysisResultV3,
)
from assurance_improvement.contracts.attempts import (
    select_analysis_slice,
    select_evaluate_memory,
    select_retro_collect,
)
from assurance_improvement.contracts.delivery import artifact_digest, same_digest
from assurance_improvement.contracts.improvements import (
    ImprovementLedgerProjection,
    ImprovementProjection,
)
from assurance_improvement.contracts.review import AppliedAutoReviewV1
from assurance_improvement.contracts.retro import (
    ContextSignalSet,
    DomainAnalysisStatus,
    DomainStatuses,
    RetroCollectInput,
    RetroCollectedV1,
    RetroContextV3,
    RetroIntegrity,
    RetroBuildSlicesInputV1,
    RetroReconcileInputV1,
    RetroReconcileResultV1,
    RetroSourceManifestV3,
    Signal,
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


def _wire_value(value: object) -> object:
    """Reduce cross-wheel Pydantic values to the graph's JSON wire contract."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_wire_value(item) for item in value]
    return value


def _pick(state: Mapping[str, object], *names: str) -> dict[str, object]:
    return {name: _wire_value(state[name]) for name in names if name in state}


def select_skill(state: Mapping[str, object]) -> ImprovementSkillInputV1:
    artifact_paths = state.get("artifact_paths") or state.get("allowed_artifact_paths") or ()
    return ImprovementSkillInputV1.model_validate(
        {
            **_pick(state, *_SKILL_FIELDS),
            "artifact_paths": _wire_value(artifact_paths),
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


def select_build_slices(state: Mapping[str, object]) -> RetroBuildSlicesInputV1:
    return RetroBuildSlicesInputV1.model_validate(_pick(state, "retro_id", "window", "source_refs"))


def publish_build_slices(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    collected = RetroCollectInput.model_validate(_output_payload(output))
    return collected.model_dump(mode="json")


def publish_collect(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    collected = RetroCollectedV1.model_validate(_output_payload(output))
    return {**collected.model_dump(mode="json", exclude={"generated_at"}), "ts": collected.generated_at}


def _select_analysis(
    state: Mapping[str, object], domain: Literal["issue", "workflow", "eval"]
) -> RetroAnalysisInputV1:
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
    return RetroAnalysisInputV1.model_validate(
        {"change_id": state["change_id"], "evidence_slice": select_analysis_slice(collected, domain=domain)}
    )


def select_eval_analysis(state: Mapping[str, object]) -> RetroAnalysisInputV1:
    return _select_analysis(state, "eval")


def select_issue_analysis(state: Mapping[str, object]) -> RetroAnalysisInputV1:
    return _select_analysis(state, "issue")


def select_workflow_analysis(state: Mapping[str, object]) -> RetroAnalysisInputV1:
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


def _merge_domain_signals(domain: str, sources: tuple[tuple[Signal, ...], ...]) -> tuple[Signal, ...]:
    ordered: dict[str, Signal] = {}
    for source in sources:
        for signal in source:
            existing = ordered.get(signal.signal_id)
            if existing is None:
                ordered[signal.signal_id] = signal
            elif existing.model_dump(mode="json") != signal.model_dump(mode="json"):
                raise ValueError(f"conflicting signal_id {signal.signal_id!r} in {domain} signals")
    return tuple(ordered.values())


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
    coverage_gap_slice = collected.coverage_gap_slice
    coverage_gap_digest = artifact_digest(coverage_gap_slice) if coverage_gap_slice is not None else None
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
        if getattr(slice_, "domain", domain) != domain or signal_doc.domain != domain:
            raise ValueError(f"{domain} signal domain does not match")
        actual = artifact_digest(slice_)
        if not same_digest(actual, digest) or not same_digest(actual, signal_doc.slice_sha256):
            raise ValueError(f"{domain} slice digest is not authenticated")
        if signal_doc.analysis_status == "failed":
            statuses[domain] = DomainAnalysisStatus(status="failed", failure_reason=signal_doc.failure_reason)
            signals[domain] = slice_.deterministic_signals
            reasons.append(f"{domain}_signal_analysis_failed")
        else:
            statuses[domain] = DomainAnalysisStatus(status="ok")
            signals[domain] = _merge_domain_signals(
                domain, (slice_.deterministic_signals, signal_doc.signals)
            )
        for reason in slice_.integrity.reasons:
            if reason not in reasons:
                reasons.append(reason)
    if coverage_gap_slice is not None:
        # The coverage_gap slice is collected but has no analyzer in this graph.
        # Its integrity reasons still have to reach the context, otherwise a
        # corrupt or out-of-window projection is invisible to every consumer.
        for reason in coverage_gap_slice.integrity.reasons:
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
            coverage_gap_slice_sha256=coverage_gap_digest,
            issue_sources=issue_slice.sources,
            workflow_sources=workflow_slice.sources,
            eval_sources=evaluation.sources,
            coverage_gap_sources=coverage_gap_slice.sources if coverage_gap_slice is not None else (),
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
            coverage_gap=(DomainAnalysisStatus(status="skipped") if coverage_gap_slice is not None else None),
        ),
        signals=ContextSignalSet.model_validate(
            {"issue": signals["issue"], "workflow": signals["workflow"], "eval": signals["eval"]}
        ),
        signal_count=sum(len(items) for items in signals.values()),
    )
    return {"context": context.model_dump(mode="json"), "candidates": []}


def select_reconcile(state: Mapping[str, object]):
    return RetroReconcileInputV1.model_validate(_pick(state, "change_id", "context", "candidates"))


def publish_reconcile(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    output_result = RetroReconcileResultV1.model_validate(_output_payload(output))
    result = output_result.reconciliation
    ledger = ImprovementLedgerProjection(
        schema_version=result.schema_version,
        last_seq=result.last_seq,
        improvements=result.improvements,
        by_fingerprint=result.by_fingerprint,
    )
    return {
        "ledger": ledger.model_dump(mode="json"),
        "retro_status": output_result.status.model_dump(mode="json"),
        "evidence_refs": [ref.model_dump(mode="json") for ref in output_result.artifact_refs],
    }


def select_retro(state: Mapping[str, object]) -> RetroSynthesisInputV1:
    return RetroSynthesisInputV1.model_validate(_pick(state, "change_id", "context"))


def publish_retro(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state, receipt
    payload = RetroAnalysisResultV3.model_validate(_output_payload(output))
    return {
        "candidates": [item.model_dump(mode="json") for item in payload.candidates],
        "analysis_status": payload.analysis_status,
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
    del state, receipt
    result = AppliedAutoReviewV1.model_validate(_output_payload(output))
    projection = result.projection.model_dump(mode="json")
    return {
        "lifecycle_state": result.projection.state.value,
        "projection": projection,
        "current": projection,
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
    del state, receipt
    result = ImprovementProjection.model_validate(_output_payload(output))
    projection = result.model_dump(mode="json")
    return {
        "lifecycle_state": result.state.value,
        "projection": projection,
        "current": projection,
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
    "publish_build_slices",
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
    "select_build_slices",
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
