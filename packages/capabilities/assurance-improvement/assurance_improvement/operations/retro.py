"""Deterministic retro collection, assembly, fallback, and status handlers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime_contracts.ops import InputError, failed_input
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import RetroAnalysisResultV3
from assurance_improvement.contracts.delivery import artifact_digest, same_digest
from assurance_improvement.contracts.improvements import (
    ImprovementCandidateV3,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementState,
)
from assurance_improvement.contracts.knowledge import to_persisted_data_knowledge_proposal
from assurance_improvement.contracts.retro import (
    ContextSignalSet,
    CoverageGapEvidenceSlice,
    DiscoveryEvidenceSlice,
    DomainAnalysisStatus,
    DomainStatuses,
    EvalEvidenceSlice,
    ImprovementCandidateDocumentV3,
    IssueEvidenceSlice,
    RetroCollectInput,
    RetroContextV3,
    RetroIntegrity,
    RetroReconcileInputV1,
    RetroSynthesizeInputV1,
    RetroSynthesizeV1,
    RetroSourceManifestV3,
    RetroWindow,
    Signal,
    SignalDocumentV3,
    WorkflowEvidenceSlice,
)
from assurance_improvement.operations.common import succeeded, validate_input
from assurance_improvement.operations.keys import (
    improvement_event_id,
    improvement_fingerprint,
    improvement_id_for_fingerprint,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class AssembleRetroInput(BaseModel):
    model_config = _FROZEN

    generated_at: str = Field(min_length=1)
    dry_run: bool = False
    window: RetroWindow
    issue_slice: IssueEvidenceSlice
    workflow_slice: WorkflowEvidenceSlice
    eval_slice: EvalEvidenceSlice
    discovery_slice: DiscoveryEvidenceSlice | None = None
    coverage_gap_slice: CoverageGapEvidenceSlice | None = None
    issue_signals: SignalDocumentV3
    workflow_signals: SignalDocumentV3
    eval_signals: SignalDocumentV3
    discovery_signals: SignalDocumentV3 | None = None
    coverage_gap_signals: SignalDocumentV3 | None = None
    issue_slice_sha256: str = Field(min_length=1)
    workflow_slice_sha256: str = Field(min_length=1)
    eval_slice_sha256: str = Field(min_length=1)
    discovery_slice_sha256: str | None = None
    coverage_gap_slice_sha256: str | None = None


class ReconcileInput(BaseModel):
    model_config = _FROZEN

    context: RetroContextV3
    candidates: tuple[ImprovementCandidateV3, ...] = ()
    current: ImprovementLedgerProjection
    ts: str = Field(min_length=1)


def analysis_slice(
    collected: RetroCollectInput,
    domain: Literal["issue", "workflow", "eval"],
) -> IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice:
    if domain == "issue":
        return collected.issue_slice
    if domain == "workflow":
        return collected.workflow_slice
    return collected.eval_slice


def _merge_domain_signals(domain: str, sources: tuple[tuple[Signal, ...], ...]) -> tuple[Signal, ...]:
    ordered: dict[str, Signal] = {}
    for source in sources:
        for signal in source:
            existing = ordered.get(signal.signal_id)
            if existing is None:
                ordered[signal.signal_id] = signal
            elif existing.model_dump(mode="json") != signal.model_dump(mode="json"):
                raise InputError(f"conflicting signal_id {signal.signal_id!r} in {domain} signals")
    return tuple(ordered.values())


def assert_collect_identity(payload: RetroCollectInput) -> None:
    slices = (
        payload.issue_slice,
        payload.workflow_slice,
        payload.eval_slice,
        payload.discovery_slice,
        payload.coverage_gap_slice,
    )
    retro_ids: set[str] = set()
    for slice_ in slices:
        if slice_ is None:
            continue
        if slice_.retro_id != payload.retro_id:
            raise InputError("slice retro_id does not match")
        if slice_.window != payload.window:
            raise InputError("slice window does not match")
        retro_ids.add(slice_.retro_id)
    if len(retro_ids) != 1:
        raise InputError("collect requires a single retro_id")


def assemble_context(payload: AssembleRetroInput) -> RetroContextV3:
    required = {
        "issue": (payload.issue_slice, payload.issue_signals, payload.issue_slice_sha256),
        "workflow": (payload.workflow_slice, payload.workflow_signals, payload.workflow_slice_sha256),
        "eval": (payload.eval_slice, payload.eval_signals, payload.eval_slice_sha256),
    }
    optional = {
        "discovery": (payload.discovery_slice, payload.discovery_signals, payload.discovery_slice_sha256),
        "coverage_gap": (
            payload.coverage_gap_slice,
            payload.coverage_gap_signals,
            payload.coverage_gap_slice_sha256,
        ),
    }
    coverage_gap_slice = payload.coverage_gap_slice
    unanalyzed: dict[str, tuple[CoverageGapEvidenceSlice, str]] = {}
    if coverage_gap_slice is not None and payload.coverage_gap_signals is None:
        # No analyzer produces coverage_gap signals yet, so an authenticated slice
        # on its own is a collected-but-unanalyzed domain, not an incomplete pair.
        coverage_gap_digest = payload.coverage_gap_slice_sha256
        if coverage_gap_digest is None:
            raise InputError("coverage_gap slice digest is not authenticated")
        unanalyzed["coverage_gap"] = (coverage_gap_slice, coverage_gap_digest)
        del optional["coverage_gap"]
    for domain, (slice_, signals, digest) in optional.items():
        present = (slice_ is not None, signals is not None, digest is not None)
        if any(present) and not all(present):
            raise InputError(f"{domain} slice/signal pair is incomplete")
        if slice_ is not None and signals is not None and digest is not None:
            required[domain] = (slice_, signals, digest)
    statuses: dict[str, DomainAnalysisStatus] = {}
    signals: dict[str, tuple[Signal, ...]] = {}
    reasons: list[str] = []
    slices: dict[str, IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice] = {}
    slice_digests: dict[str, str] = {}
    retro_ids: set[str] = set()
    for domain, (slice_, signal_doc, digest) in required.items():
        if slice_.window != payload.window or slice_.retro_id != signal_doc.retro_id:
            raise InputError(f"{domain} assembly identity mismatch")
        if getattr(slice_, "domain", domain) != domain or signal_doc.domain != domain:
            raise InputError(f"{domain} signal domain does not match")
        actual = artifact_digest(slice_)
        if not same_digest(actual, digest) or not same_digest(actual, signal_doc.slice_sha256):
            raise InputError(f"{domain} slice digest is not authenticated")
        retro_ids.add(slice_.retro_id)
        slices[domain] = slice_
        slice_digests[domain] = digest
        for reason in slice_.integrity.reasons:
            if reason not in reasons:
                reasons.append(reason)
        if signal_doc.analysis_status == "failed":
            statuses[domain] = DomainAnalysisStatus(status="failed", failure_reason=signal_doc.failure_reason)
            signals[domain] = slice_.deterministic_signals
            reasons.append(f"{domain}_signal_analysis_failed")
        else:
            statuses[domain] = DomainAnalysisStatus(status="ok")
            signals[domain] = _merge_domain_signals(
                domain, (slice_.deterministic_signals, signal_doc.signals)
            )
    for domain, (collected_slice, collected_digest) in unanalyzed.items():
        if collected_slice.window != payload.window or collected_slice.domain != domain:
            raise InputError(f"{domain} assembly identity mismatch")
        if not same_digest(artifact_digest(collected_slice), collected_digest):
            raise InputError(f"{domain} slice digest is not authenticated")
        retro_ids.add(collected_slice.retro_id)
        slice_digests[domain] = collected_digest
        statuses[domain] = DomainAnalysisStatus(status="skipped")
        for reason in collected_slice.integrity.reasons:
            if reason not in reasons:
                reasons.append(reason)
    if len(retro_ids) != 1:
        raise InputError("assembly requires a single retro_id")
    integrity = (
        RetroIntegrity(status="incomplete", reasons=tuple(dict.fromkeys(reasons)))
        if reasons
        else RetroIntegrity(status="complete")
    )
    discovery_slice = payload.discovery_slice
    return RetroContextV3(
        retro_id=next(iter(slices.values())).retro_id,
        generated_at=payload.generated_at,
        dry_run=payload.dry_run,
        window=payload.window,
        source_manifest=RetroSourceManifestV3(
            issue_slice_sha256=slice_digests["issue"],
            workflow_slice_sha256=slice_digests["workflow"],
            eval_slice_sha256=slice_digests["eval"],
            discovery_slice_sha256=slice_digests.get("discovery"),
            coverage_gap_slice_sha256=slice_digests.get("coverage_gap"),
            issue_sources=payload.issue_slice.sources,
            workflow_sources=payload.workflow_slice.sources,
            eval_sources=payload.eval_slice.sources,
            discovery_sources=discovery_slice.sources if discovery_slice is not None else (),
            coverage_gap_sources=coverage_gap_slice.sources if coverage_gap_slice is not None else (),
        ),
        integrity=integrity,
        domain_status=DomainStatuses(
            issue=statuses["issue"],
            workflow=statuses["workflow"],
            eval=statuses["eval"],
            discovery=statuses.get("discovery"),
            coverage_gap=statuses.get("coverage_gap"),
        ),
        signals=ContextSignalSet(
            issue=signals["issue"],
            workflow=signals["workflow"],
            eval=signals["eval"],
            discovery=signals.get("discovery", ()),
            coverage_gap=signals.get("coverage_gap", ()),
        ),
        signal_count=sum(len(items) for items in signals.values()),
    )


def empty_analysis(context: RetroContextV3) -> ImprovementCandidateDocumentV3:
    if context.signal_count != 0:
        raise InputError("NOOP receipt requires signal_count == 0")
    return ImprovementCandidateDocumentV3.model_validate(
        {
            "schema_version": "3",
            "retro_id": context.retro_id,
            "context_sha256": artifact_digest(context),
            "candidates": [],
        },
        context={"retro_manifest": context.source_manifest},
    )


def reconcile_improvements(payload: ReconcileInput) -> dict[str, object]:
    ImprovementCandidateDocumentV3.model_validate(
        {
            "schema_version": "3",
            "retro_id": payload.context.retro_id,
            "context_sha256": artifact_digest(payload.context),
            "candidates": [item.model_dump(mode="json") for item in payload.candidates],
        },
        context={"retro_manifest": payload.context.source_manifest},
    )
    improvements = dict(payload.current.improvements)
    by_fingerprint = dict(payload.current.by_fingerprint)
    last_seq = payload.current.last_seq
    improvement_ids: list[str] = []
    events: list[dict[str, object]] = []
    for ordinal, candidate in enumerate(payload.candidates, start=1):
        fingerprint = improvement_fingerprint(candidate)
        existing_id = by_fingerprint.get(fingerprint)
        improvement_id = existing_id or improvement_id_for_fingerprint(fingerprint)
        if existing_id is not None:
            existing = improvements[existing_id]
            incoming = set(candidate.source_refs.all_ids())
            if incoming.issubset(existing.source_refs.all_ids()):
                improvement_ids.append(existing_id)
                continue
            last_seq += 1
            merged = ImprovementSourceRefs.model_validate(
                {
                    field: tuple(
                        sorted(
                            set(getattr(existing.source_refs, field))
                            | set(getattr(candidate.source_refs, field))
                        )
                    )
                    for field in (
                        "problem_ids",
                        "occurrence_ids",
                        "issue_event_ids",
                        "workflow_evidence_ids",
                        "eval_run_ids",
                    )
                }
            )
            event_id = improvement_event_id(payload.context.retro_id, "improvement_evidence_linked", ordinal)
            events.append(
                {
                    "type": "improvement_evidence_linked",
                    "event_id": event_id,
                    "improvement_id": existing_id,
                    "seq": last_seq,
                }
            )
            improvements[existing_id] = existing.model_copy(
                update={
                    "source_refs": merged,
                    "version": existing.version + 1,
                    "last_event_id": event_id,
                    "proposed_by_retro_ids": tuple(
                        sorted(set(existing.proposed_by_retro_ids) | {payload.context.retro_id})
                    ),
                }
            )
            improvement_ids.append(existing_id)
            continue
        if candidate.kind is ImprovementKind.DOMAIN_KNOWLEDGE and not payload.context.allows_domain_knowledge:
            raise InputError("domain_knowledge requires complete retro integrity")
        last_seq += 1
        event_id = improvement_event_id(payload.context.retro_id, "improvement_proposed", ordinal)
        projection = ImprovementProjection(
            improvement_id=improvement_id,
            fingerprint=fingerprint,
            kind=candidate.kind,
            delivery=candidate.delivery,
            source_refs=candidate.source_refs,
            target=candidate.target,
            rationale=candidate.rationale,
            proposed_change=candidate.proposed_change,
            knowledge_delta=to_persisted_data_knowledge_proposal(candidate.knowledge_delta),
            verification=candidate.verification,
            risk=candidate.risk,
            confidence=candidate.confidence,
            state=ImprovementState.PROPOSED,
            version=1,
            proposed_by_retro_ids=(payload.context.retro_id,),
            supersedes=candidate.supersedes,
            last_event_id=event_id,
        )
        improvements[improvement_id] = projection
        by_fingerprint[fingerprint] = improvement_id
        events.append(
            {
                "type": "improvement_proposed",
                "event_id": event_id,
                "improvement_id": improvement_id,
                "seq": last_seq,
            }
        )
        if candidate.supersedes and candidate.supersedes in improvements:
            predecessor = improvements[candidate.supersedes]
            last_seq += 1
            supersede_id = improvement_event_id(payload.context.retro_id, "improvement_superseded", ordinal)
            improvements[candidate.supersedes] = predecessor.model_copy(
                update={
                    "state": ImprovementState.SUPERSEDED,
                    "version": predecessor.version + 1,
                    "last_event_id": supersede_id,
                }
            )
            events.append(
                {
                    "type": "improvement_superseded",
                    "event_id": supersede_id,
                    "improvement_id": candidate.supersedes,
                    "seq": last_seq,
                    "superseded_by": improvement_id,
                }
            )
        improvement_ids.append(improvement_id)
    return {
        "schema_version": "1",
        "last_seq": last_seq,
        "improvements": {key: value.model_dump(mode="json") for key, value in improvements.items()},
        "by_fingerprint": by_fingerprint,
        "improvement_ids": improvement_ids,
        "events": events,
    }


def validate_candidates(
    context: RetroContextV3, candidates: tuple[ImprovementCandidateV3, ...]
) -> ImprovementCandidateDocumentV3:
    # The graph and the validator may arrive from independently installed
    # wheels.  Reconstruct the manifest at this contract boundary so the
    # document validator never relies on Python class identity across wheels.
    manifest = RetroSourceManifestV3.model_validate(context.source_manifest.model_dump(mode="json"))
    document = ImprovementCandidateDocumentV3.model_validate(
        {
            "retro_id": context.retro_id,
            "context_sha256": artifact_digest(context),
            "candidates": [item.model_dump(mode="json") for item in candidates],
        },
        context={"retro_manifest": manifest},
    )
    signals = {
        signal.signal_id: signal
        for domain in type(context.signals).model_fields
        for signal in getattr(context.signals, domain)
    }
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise InputError("candidate ids must be unique")
    for candidate in candidates:
        if any(signal_id not in signals for signal_id in candidate.signal_ids):
            raise InputError("candidate cites a signal outside the locked context")
        supported = {
            ref for signal_id in candidate.signal_ids for ref in signals[signal_id].source_refs.all_ids()
        }
        if not set(candidate.source_refs.all_ids()).issubset(supported):
            raise InputError("candidate sources must be supported by its cited signals")
        if candidate.kind is ImprovementKind.DOMAIN_KNOWLEDGE and not context.allows_domain_knowledge:
            raise InputError("domain_knowledge requires complete retro integrity")
    return document


class RetroCollectHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.handoff import COLLECTED
        from assurance_improvement.contracts.retro import (
            CoverageGapEvidenceSlice,
            DiscoveryEvidenceSlice,
            EvalEvidenceSlice,
            IssueEvidenceSlice,
            RetroCollectAttemptInput,
            RetroCollectedStamp,
            WorkflowEvidenceSlice,
        )
        from assurance_improvement.operations.files import load_named, stage_named

        try:
            attempt = validate_input(RetroCollectAttemptInput, request.input)
            payload = RetroCollectInput(
                retro_id=attempt.retro_id,
                window=attempt.window,
                issue_slice=load_named(context, attempt.issue_slice_ref, IssueEvidenceSlice),
                workflow_slice=load_named(context, attempt.workflow_slice_ref, WorkflowEvidenceSlice),
                eval_slice=load_named(context, attempt.eval_slice_ref, EvalEvidenceSlice),
                discovery_slice=(
                    load_named(context, attempt.discovery_slice_ref, DiscoveryEvidenceSlice)
                    if attempt.discovery_slice_ref is not None
                    else None
                ),
                coverage_gap_slice=(
                    load_named(context, attempt.coverage_gap_slice_ref, CoverageGapEvidenceSlice)
                    if attempt.coverage_gap_slice_ref is not None
                    else None
                ),
            )
            assert_collect_identity(payload)
            generated_at = datetime.now(timezone.utc).isoformat()
            stage_named(context, COLLECTED, RetroCollectedStamp(generated_at=generated_at))
            return succeeded(
                {
                    "retro_id": payload.retro_id,
                    "generated_at": generated_at,
                    "window": payload.window.model_dump(mode="json"),
                    "issue_slice": payload.issue_slice.model_dump(mode="json"),
                    "workflow_slice": payload.workflow_slice.model_dump(mode="json"),
                    "eval_slice": payload.eval_slice.model_dump(mode="json"),
                    "discovery_slice": (
                        payload.discovery_slice.model_dump(mode="json") if payload.discovery_slice else None
                    ),
                    "coverage_gap_slice": (
                        payload.coverage_gap_slice.model_dump(mode="json")
                        if payload.coverage_gap_slice
                        else None
                    ),
                }
            )
        except InputError as error:
            return failed_input(error)


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


def route_retro_synthesis(
    payload: RetroSynthesizeInputV1,
    analyses: Mapping[str, Mapping[str, object]],
) -> RetroSynthesizeV1:
    """Hash the collected slices, merge signals, and choose synthesize or empty."""
    domains = (
        ("issue", payload.issue_slice, analyses["issue"]),
        ("workflow", payload.workflow_slice, analyses["workflow"]),
        ("eval", payload.eval_slice, analyses["eval"]),
    )
    digests: dict[str, str] = {}
    documents: dict[str, SignalDocumentV3] = {}
    for domain, slice_, analysis in domains:
        digest = artifact_digest(slice_)
        digests[domain] = digest
        documents[domain] = SignalDocumentV3.model_validate(
            _signal_document(
                analysis,
                domain=domain,
                retro_id=payload.retro_id,
                slice_sha256=digest,
            )
        )
    coverage_gap = payload.coverage_gap_slice
    context = assemble_context(
        AssembleRetroInput(
            generated_at=payload.generated_at,
            dry_run=payload.dry_run,
            window=payload.window,
            issue_slice=payload.issue_slice,
            workflow_slice=payload.workflow_slice,
            eval_slice=payload.eval_slice,
            discovery_slice=payload.discovery_slice,
            coverage_gap_slice=coverage_gap,
            issue_signals=documents["issue"],
            workflow_signals=documents["workflow"],
            eval_signals=documents["eval"],
            issue_slice_sha256=digests["issue"],
            workflow_slice_sha256=digests["workflow"],
            eval_slice_sha256=digests["eval"],
            coverage_gap_slice_sha256=artifact_digest(coverage_gap) if coverage_gap is not None else None,
        )
    )
    return RetroSynthesizeV1(
        route="synthesize" if context.signal_count else "empty",
        context=context,
        candidates=(),
    )


def _load_analysis(root: Path, ref: object) -> dict[str, object]:
    path = getattr(ref, "path", None)
    digest = getattr(ref, "digest", None)
    if not isinstance(path, str) or not isinstance(digest, str):
        raise InputError("analysis artifact ref is missing")
    file_path = root / path
    try:
        raw = file_path.read_bytes()
    except OSError as error:
        raise InputError(f"analysis artifact is missing: {path}") from error
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise InputError(f"analysis artifact digest does not match: {path}")
    try:
        document = RetroAnalysisResultV3.model_validate_json(raw)
    except (ValidationError, ValueError) as error:
        raise InputError(f"analysis artifact is invalid: {path}") from error
    return document.model_dump(mode="json")


class RetroSynthesizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.handoff import CONTEXT
        from assurance_improvement.contracts.retro import (
            CoverageGapEvidenceSlice,
            DiscoveryEvidenceSlice,
            EvalEvidenceSlice,
            IssueEvidenceSlice,
            RetroCollectedStamp,
            RetroSynthesizeAttemptInput,
            WorkflowEvidenceSlice,
        )
        from assurance_improvement.operations.files import load_named, stage_named

        try:
            attempt = validate_input(RetroSynthesizeAttemptInput, request.input)
            stamp = load_named(context, attempt.generated_at_ref, RetroCollectedStamp)
            payload = RetroSynthesizeInputV1(
                generated_at=stamp.generated_at,
                dry_run=attempt.dry_run,
                retro_id=attempt.retro_id,
                window=attempt.window,
                issue_slice=load_named(context, attempt.issue_slice_ref, IssueEvidenceSlice),
                workflow_slice=load_named(context, attempt.workflow_slice_ref, WorkflowEvidenceSlice),
                eval_slice=load_named(context, attempt.eval_slice_ref, EvalEvidenceSlice),
                discovery_slice=(
                    load_named(context, attempt.discovery_slice_ref, DiscoveryEvidenceSlice)
                    if attempt.discovery_slice_ref is not None
                    else None
                ),
                coverage_gap_slice=(
                    load_named(context, attempt.coverage_gap_slice_ref, CoverageGapEvidenceSlice)
                    if attempt.coverage_gap_slice_ref is not None
                    else None
                ),
                issue_analysis_ref=attempt.issue_analysis_ref,
                workflow_analysis_ref=attempt.workflow_analysis_ref,
                eval_analysis_ref=attempt.eval_analysis_ref,
            )
            root = Path(context.project_root)
            analyses = {
                "issue": _load_analysis(root, payload.issue_analysis_ref),
                "workflow": _load_analysis(root, payload.workflow_analysis_ref),
                "eval": _load_analysis(root, payload.eval_analysis_ref),
            }
            routed = route_retro_synthesis(payload, analyses)
            stage_named(context, CONTEXT, routed.context)
            return succeeded(cast(dict[str, object], routed.model_dump(mode="json")))
        except (InputError, ValidationError, OSError, ValueError) as error:
            return failed_input(error)


class ReconcileImprovementsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.retro import RetroCandidatesFile, RetroReconcileAttemptInput
        from assurance_improvement.operations.files import load_named
        from assurance_improvement.operations.retro_persistence import stage_reconciliation

        try:
            attempt = validate_input(RetroReconcileAttemptInput, request.input)
            candidates = (
                load_named(context, attempt.candidates_ref, RetroCandidatesFile).candidates
                if attempt.candidates_ref is not None
                else ()
            )
            payload = RetroReconcileInputV1(
                change_id=attempt.change_id,
                context=load_named(context, attempt.context_ref, RetroContextV3),
                candidates=candidates,
            )
            result = stage_reconciliation(payload, context)
            return succeeded(cast(dict[str, object], result.model_dump(mode="json")))
        except (InputError, ValidationError, OSError, ValueError) as error:
            return failed_input(error)


__all__ = [
    "ReconcileImprovementsHandler",
    "RetroSynthesizeHandler",
    "route_retro_synthesis",
    "RetroCollectHandler",
    "analysis_slice",
    "assemble_context",
    "assert_collect_identity",
    "empty_analysis",
    "reconcile_improvements",
]
