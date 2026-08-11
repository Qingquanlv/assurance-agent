"""Deterministic Retro fallback artifacts and process Improvements."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.artifacts.models.retro_batch import (
    RetroBatchScope,
    RetroPipelineFailure,
    RetroPipelineFailureDocument,
)
from assurance_agent.artifacts.models.retro_v3 import (
    BatchMemberEvidenceGapSignal,
    ConfirmedEscapeSignal,
    ContextSignalSet,
    DomainAnalysisStatus,
    DomainStatuses,
    EvalEvidenceSlice,
    ImprovementCandidateDocumentV3,
    ImprovementCandidateV3,
    IssueEvidenceSlice,
    ReopenedCoverageGapSignal,
    RetroContextV3,
    RetroIntegrity,
    RetroPipelineFailureSignal,
    RetroSelectionSnapshot,
    RetroSourceDescriptor,
    RetroSourceManifestV3,
    RetroWindow,
    SignalDocumentV3,
    WorkflowEvidenceSlice,
)
from assurance_agent.retro.assemble import agent_context_json_bytes, assemble_context


def _stable_id(prefix: str, payload: object) -> str:
    digest = sha256_bytes(canonical_json_bytes(payload)).removeprefix("sha256:")
    return f"{prefix}-{digest[:24]}"


def candidate_from_evidence_gaps(
    *,
    retro_id: str,
    batch_scope: RetroBatchScope,
    gaps: Sequence[BatchMemberEvidenceGapSignal],
    context_sha256: str,
) -> ImprovementCandidateV3:
    """Build one stable workflow Improvement without an LLM call."""
    del retro_id, context_sha256
    ordered = tuple(
        sorted(gaps, key=lambda item: (item.change_id, item.domain, item.reason_code, item.signal_id))
    )
    gap_key = tuple(
        (item.change_id, item.execution_status, item.domain, item.reason_code) for item in ordered
    )
    signal_ids = tuple(sorted({item.signal_id for item in ordered}))
    return ImprovementCandidateV3(
        candidate_id=_stable_id("CAND-GAP", {"batch_id": batch_scope.batch_id, "gaps": gap_key}),
        kind=ImprovementKind.WORKFLOW,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(workflow_evidence_ids=signal_ids),
        target="assurance-agent:retro:evidence-collection",
        rationale="A declared Retro batch member lacks complete, immutable evidence.",
        proposed_change=(
            "Make Batch evidence publication durable and independently recoverable for: "
            + ", ".join(":".join(item) for item in gap_key)
        ),
        verification=ImprovementVerification(
            suites=("retro-batch-evidence",),
            required_cases=tuple(item.change_id for item in ordered),
            success_criteria="Every declared Batch member resolves to complete immutable evidence or a typed gap.",
        ),
        risk="medium",
        confidence="high",
        signal_ids=signal_ids,
    )


def coverage_gap_fingerprint(signal: ReopenedCoverageGapSignal) -> str:
    """Stable fingerprint = gap kind + locator axes (batch/change independent)."""
    return signal.locator_fingerprint


def candidate_from_coverage_gaps(
    *,
    signals: Sequence[ReopenedCoverageGapSignal],
    retro_id: str,
) -> ImprovementCandidateV3:
    """Build one ``test_improvement`` candidate from reopened coverage-gap signal(s).

    Fingerprint / candidate_id are derived from kind+locator only so the same gap
    across batches does not create duplicate candidates.
    """
    del retro_id
    if not signals:
        raise ValueError("coverage-gap candidate requires at least one signal")
    ordered = tuple(
        sorted(signals, key=lambda item: (item.locator_fingerprint, item.change_id, item.signal_id))
    )
    primary = ordered[0]
    fingerprint = coverage_gap_fingerprint(primary)
    signal_ids = tuple(sorted({item.signal_id for item in ordered}))
    evidence_ids = tuple(sorted({eid for item in ordered for eid in item.source_refs.workflow_evidence_ids}))
    return ImprovementCandidateV3(
        candidate_id=_stable_id("CAND-GAP-COV", {"fingerprint": fingerprint}),
        kind=ImprovementKind.TEST,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(workflow_evidence_ids=evidence_ids or signal_ids),
        target=f"assurance-agent:retro:coverage-gap:{fingerprint}",
        rationale=f"Reopened coverage gap {fingerprint} requires durable regression coverage.",
        proposed_change=(
            f"Add or restore a regression case closing gap kind={primary.gap_kind} locator={fingerprint}."
        ),
        verification=ImprovementVerification(
            suites=("coverage-gap-closure",),
            required_cases=(primary.case_id,) if primary.case_id else (fingerprint,),
            success_criteria="The reopened coverage-gap identity is absent from the next projection.",
        ),
        risk="medium",
        confidence="high",
        signal_ids=signal_ids,
    )


def candidate_from_confirmed_escape(
    *,
    signal: ConfirmedEscapeSignal,
    retro_id: str,
) -> ImprovementCandidateV3:
    """Escape with missed obligations → domain_knowledge; otherwise test_improvement."""
    del retro_id
    missed = tuple(signal.missed_obligation_ids)
    fingerprint = "escape|" + signal.problem_id + "|" + ",".join(missed)
    if missed:
        entity_key = signal.problem_id.replace("/", "_")
        knowledge = DataKnowledgeProposal.model_validate(
            {
                "schema_version": "1",
                "mode": "delta",
                "entities": {
                    entity_key: {
                        "notes": (
                            f"Confirmed escape {signal.problem_id}; missed obligations: " + ", ".join(missed)
                        ),
                        "required_fields": list(missed),
                    }
                },
                "needs_review": [f"escape:{signal.problem_id}"],
            }
        )
        return ImprovementCandidateV3(
            candidate_id=_stable_id("CAND-ESCAPE-DK", {"fingerprint": fingerprint}),
            kind=ImprovementKind.DOMAIN_KNOWLEDGE,
            delivery=DeliveryKind.KNOWLEDGE_DELTA,
            source_refs=ImprovementSourceRefs(problem_ids=(signal.problem_id,)),
            target=f"assurance-agent:retro:escape:{signal.problem_id}",
            rationale=f"Confirmed escape {signal.problem_id} missed declared obligations.",
            proposed_change=(
                "Record missed obligations in domain knowledge so subsequent discovery "
                "and case design cannot omit them."
            ),
            knowledge_delta=knowledge,
            verification=ImprovementVerification(
                suites=("escape-obligation-coverage",),
                required_cases=(signal.problem_id,),
                success_criteria="Missed obligations appear in accepted domain knowledge.",
            ),
            risk="medium",
            confidence="high",
            signal_ids=(signal.signal_id,),
        )
    return ImprovementCandidateV3(
        candidate_id=_stable_id("CAND-ESCAPE-TEST", {"fingerprint": fingerprint}),
        kind=ImprovementKind.TEST,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(problem_ids=(signal.problem_id,)),
        target=f"assurance-agent:retro:escape-test:{signal.problem_id}",
        rationale=f"Confirmed escape {signal.problem_id} needs regression coverage.",
        proposed_change=f"Add a regression test covering confirmed escape {signal.problem_id}.",
        verification=ImprovementVerification(
            suites=("escape-regression",),
            required_cases=(signal.problem_id,),
            success_criteria="Confirmed escape is covered by an automated regression case.",
        ),
        risk="medium",
        confidence="high",
        signal_ids=(signal.signal_id,),
    )


def candidate_fingerprint(candidate: ImprovementCandidateV3) -> str:
    """Recover the fallback fingerprint embedded in ``target`` (kind+locator / escape id)."""
    for prefix in (
        "assurance-agent:retro:coverage-gap:",
        "assurance-agent:retro:escape:",
        "assurance-agent:retro:escape-test:",
    ):
        if candidate.target.startswith(prefix):
            return candidate.target.removeprefix(prefix)
    return candidate.candidate_id


def dedupe_candidates_by_fingerprint(
    candidates: Sequence[ImprovementCandidateV3],
) -> tuple[ImprovementCandidateV3, ...]:
    """Keep the first candidate per fingerprint (stable input order).

    Fallback templates encode fingerprint into ``candidate_id`` / ``target`` so the
    same kind+locator across batches collapses without a new schema field.
    """
    seen: set[str] = set()
    out: list[ImprovementCandidateV3] = []
    for candidate in candidates:
        key = candidate_fingerprint(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return tuple(out)


def candidate_from_pipeline_failure(
    failure: RetroPipelineFailure,
    *,
    context_sha256: str,
) -> ImprovementCandidateV3:
    """Build a process Candidate whose identity excludes free-form failure messages."""
    del context_sha256
    identity = {"stage": failure.stage, "error_kind": failure.error_kind}
    return ImprovementCandidateV3(
        candidate_id=_stable_id("CAND-PIPELINE", identity),
        kind=ImprovementKind.WORKFLOW,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(workflow_evidence_ids=(failure.failure_id,)),
        target=f"assurance-agent:retro:{failure.stage}",
        rationale=f"Retro stage {failure.stage} failed with typed error {failure.error_kind}.",
        proposed_change=(f"Harden Retro stage {failure.stage} recovery for error kind {failure.error_kind}."),
        verification=ImprovementVerification(
            suites=("retro-pipeline-recovery",),
            required_cases=(f"{failure.stage}:{failure.error_kind}",),
            success_criteria="The failed stage reaches a persisted Retro status through deterministic recovery.",
        ),
        risk="medium",
        confidence="high",
        signal_ids=(failure.failure_id,),
    )


def _pipeline_failure_summary(
    context: RetroContextV3,
    signal: RetroPipelineFailureSignal,
    candidate: ImprovementCandidateV3,
) -> str:
    return (
        f"# Retro {context.retro_id}\n\n"
        "result: pipeline_failure_improvement\n"
        "signal_count: 1\n"
        "candidate_count: 1\n\n"
        "## Signal\n\n"
        f"- {signal.signal_id} (`{signal.signal_type}`; stage=`{signal.stage}`; "
        f"error_kind=`{signal.error_kind}`)\n\n"
        "## Candidate\n\n"
        f"- {candidate.candidate_id} (`{candidate.kind}` -> `{candidate.delivery}`; "
        f"target=`{candidate.target}`)\n"
    )


def materialize_pipeline_failure_fallback(
    project_root: Path,
    *,
    failure: RetroPipelineFailure,
    batch_scope: RetroBatchScope | None,
) -> tuple[RetroContextV3, ImprovementCandidateV3]:
    """Write a minimal digest-valid Retro run around one typed pipeline failure."""
    retro_dir = project_root / "qa" / "retro" / failure.retro_id
    evidence_dir = retro_dir / "evidence"
    signals_dir = retro_dir / "signals"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    signals_dir.mkdir(parents=True, exist_ok=True)
    change_ids = tuple(member.change_id for member in batch_scope.members) if batch_scope is not None else ()
    window = RetroWindow(
        selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=change_ids),
        change_ids=change_ids,
        batch_scope=batch_scope,
    )
    failure_doc = RetroPipelineFailureDocument(retro_id=failure.retro_id, failures=(failure,))
    failure_sha = sha256_bytes(canonical_json_bytes(failure_doc))
    failure_signal = RetroPipelineFailureSignal(
        signal_id=failure.failure_id,
        summary=f"Retro stage {failure.stage} failed",
        occurrence_count=1,
        recommended_change=f"Harden recovery for {failure.stage}:{failure.error_kind}.",
        source_refs=ImprovementSourceRefs(workflow_evidence_ids=(failure.failure_id,)),
        confidence="high",
        failure_id=failure.failure_id,
        stage=failure.stage,
        error_kind=failure.error_kind,
    )
    reason = f"retro_pipeline_failure:{failure.stage}:{failure.error_kind}"
    integrity = RetroIntegrity(status="incomplete", reasons=(reason,))
    issue_slice = IssueEvidenceSlice(retro_id=failure.retro_id, window=window, integrity=integrity)
    workflow_source = RetroSourceDescriptor(
        kind="retro_pipeline_failure",
        sha256=failure_sha,
        evidence_ids=(failure.failure_id,),
    )
    workflow_slice = WorkflowEvidenceSlice(
        retro_id=failure.retro_id,
        window=window,
        sources=(workflow_source,),
        integrity=integrity,
        deterministic_signals=(failure_signal,),
    )
    eval_slice = EvalEvidenceSlice(retro_id=failure.retro_id, window=window, integrity=integrity)
    slices = {"issue": issue_slice, "workflow": workflow_slice, "eval": eval_slice}
    slice_bytes = {domain: canonical_json_bytes(slice_) for domain, slice_ in slices.items()}
    for domain, data in slice_bytes.items():
        (evidence_dir / f"{domain}-slice.json").write_bytes(data)
        signal_doc = SignalDocumentV3(
            retro_id=failure.retro_id,
            domain=domain,  # type: ignore[arg-type]
            analysis_status="failed",
            failure_reason=reason,
            analyzer="operation:retro-pipeline-fallback",
            signals=(),
            slice_sha256=sha256_bytes(data),
        )
        (signals_dir / f"{domain}.json").write_bytes(canonical_json_bytes(signal_doc))
    generated_at = failure.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    failed_status = DomainAnalysisStatus(status="failed", failure_reason=reason)
    context = RetroContextV3(
        retro_id=failure.retro_id,
        generated_at=generated_at,
        window=window,
        source_manifest=RetroSourceManifestV3(
            issue_slice_sha256=sha256_bytes(slice_bytes["issue"]),
            workflow_slice_sha256=sha256_bytes(slice_bytes["workflow"]),
            eval_slice_sha256=sha256_bytes(slice_bytes["eval"]),
            workflow_sources=(workflow_source,),
        ),
        integrity=integrity,
        domain_status=DomainStatuses(issue=failed_status, workflow=failed_status, eval=failed_status),
        signals=ContextSignalSet(workflow=(failure_signal,)),
        signal_count=1,
    )
    context_bytes = canonical_json_bytes(context)
    candidate = candidate_from_pipeline_failure(failure, context_sha256=sha256_bytes(context_bytes))
    candidate_doc = ImprovementCandidateDocumentV3(
        retro_id=failure.retro_id,
        context_sha256=sha256_bytes(context_bytes),
        candidates=(candidate,),
    )
    (retro_dir / "pipeline-failure.json").write_bytes(canonical_json_bytes(failure_doc))
    (retro_dir / "window.json").write_bytes(canonical_json_bytes(window))
    (retro_dir / "context.json").write_bytes(context_bytes)
    (retro_dir / "context-agent.json").write_bytes(agent_context_json_bytes(context))
    (retro_dir / "proposal-candidates.json").write_bytes(canonical_json_bytes(candidate_doc))
    (retro_dir / "retro-summary.md").write_text(
        _pipeline_failure_summary(context, failure_signal, candidate),
        encoding="utf-8",
    )
    return context, candidate


def materialize_evidence_gap_fallback(
    retro_dir: Path,
    *,
    now,
) -> tuple[RetroContextV3, ImprovementCandidateV3]:
    """Assemble slice-owned gap signals and write one no-agent Candidate."""
    slice_types = {
        "issue": IssueEvidenceSlice,
        "workflow": WorkflowEvidenceSlice,
        "eval": EvalEvidenceSlice,
    }
    gaps: list[BatchMemberEvidenceGapSignal] = []
    for domain, model in slice_types.items():
        path = retro_dir / "evidence" / f"{domain}-slice.json"
        slice_ = model.model_validate_json(path.read_text(encoding="utf-8"))
        gaps.extend(
            signal
            for signal in slice_.deterministic_signals
            if isinstance(signal, BatchMemberEvidenceGapSignal)
        )
        data = path.read_bytes()
        doc = SignalDocumentV3(
            retro_id=slice_.retro_id,
            domain=domain,  # type: ignore[arg-type]
            analysis_status="ok",
            analyzer="operation:retro-evidence-gap-fallback",
            signals=(),
            slice_sha256=sha256_bytes(data),
        )
        signal_path = retro_dir / "signals" / f"{domain}.json"
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_bytes(canonical_json_bytes(doc))
    context = assemble_context(retro_dir, dry_run=False, now=now)
    if context.window.batch_scope is None or not gaps:
        raise ValueError("evidence-gap fallback requires a Batch scope and typed gaps")
    context_bytes = canonical_json_bytes(context)
    candidate = candidate_from_evidence_gaps(
        retro_id=context.retro_id,
        batch_scope=context.window.batch_scope,
        gaps=gaps,
        context_sha256=sha256_bytes(context_bytes),
    )
    document = ImprovementCandidateDocumentV3(
        retro_id=context.retro_id,
        context_sha256=sha256_bytes(context_bytes),
        candidates=(candidate,),
    )
    (retro_dir / "context.json").write_bytes(context_bytes)
    (retro_dir / "context-agent.json").write_bytes(agent_context_json_bytes(context))
    (retro_dir / "proposal-candidates.json").write_bytes(canonical_json_bytes(document))
    (retro_dir / "retro-summary.md").write_text(
        f"# Retro {context.retro_id}\n\nresult: evidence_gap_improvement\n",
        encoding="utf-8",
    )
    return context, candidate


__all__ = [
    "candidate_from_confirmed_escape",
    "candidate_from_coverage_gaps",
    "candidate_from_evidence_gaps",
    "candidate_from_pipeline_failure",
    "candidate_fingerprint",
    "coverage_gap_fingerprint",
    "dedupe_candidates_by_fingerprint",
    "materialize_pipeline_failure_fallback",
    "materialize_evidence_gap_fallback",
]
