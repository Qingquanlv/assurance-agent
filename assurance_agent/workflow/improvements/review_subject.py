"""Build and publish immutable, least-privilege Improvement review subjects."""

from __future__ import annotations

import os
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvement_review import (
    ImprovementReviewProvenance,
    ImprovementReviewSubject,
)
from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_agent.artifacts.models.retro_v3 import (
    ImprovementCandidateV3,
    RetroContextV3,
    RetroSourceDescriptor,
    RetroSourceManifestV3,
)
from assurance_agent.exceptions import AaError


class ImprovementReviewSubjectError(AaError):
    """The candidate cannot be bound to a complete immutable review subject."""


def _selected_sources(
    sources: tuple[RetroSourceDescriptor, ...], referenced_ids: frozenset[str]
) -> tuple[RetroSourceDescriptor, ...]:
    return tuple(source for source in sources if referenced_ids.intersection(source.evidence_ids))


def build_review_subject(
    candidate: ImprovementCandidateV3,
    context: RetroContextV3,
    *,
    improvement_id: str,
    candidate_batch_digest: str | None = None,
    pipeline_failures: tuple[RetroPipelineFailure, ...] = (),
) -> tuple[ImprovementReviewSubject, str, bytes]:
    """Resolve only candidate signal/source refs and return digest-bound bytes."""
    referenced_ids = frozenset(candidate.source_refs.all_ids())
    unresolved_refs = referenced_ids - context.source_manifest.resolvable_ids()
    if unresolved_refs:
        raise ImprovementReviewSubjectError("unresolvable source refs: " + ", ".join(sorted(unresolved_refs)))

    signals = (*context.signals.issue, *context.signals.workflow, *context.signals.eval)
    by_id = {signal.signal_id: signal for signal in signals}
    missing_signals = set(candidate.signal_ids) - set(by_id)
    if missing_signals:
        raise ImprovementReviewSubjectError("unresolvable signal ids: " + ", ".join(sorted(missing_signals)))
    selected_signals = tuple(by_id[signal_id] for signal_id in candidate.signal_ids)
    manifest = context.source_manifest
    narrowed_manifest = RetroSourceManifestV3(
        issue_slice_sha256=manifest.issue_slice_sha256,
        workflow_slice_sha256=manifest.workflow_slice_sha256,
        eval_slice_sha256=manifest.eval_slice_sha256,
        issue_sources=_selected_sources(manifest.issue_sources, referenced_ids),
        workflow_sources=_selected_sources(manifest.workflow_sources, referenced_ids),
        eval_sources=_selected_sources(manifest.eval_sources, referenced_ids),
    )
    context_digest = sha256_bytes(canonical_json_bytes(context))
    batch_digest = candidate_batch_digest or sha256_bytes(canonical_json_bytes(candidate))
    subject = ImprovementReviewSubject(
        improvement_id=improvement_id,
        kind=candidate.kind,
        delivery=candidate.delivery,
        target=candidate.target,
        rationale=candidate.rationale,
        proposed_change=candidate.proposed_change,
        verification=candidate.verification,
        risk=candidate.risk,
        confidence=candidate.confidence,
        source_refs=candidate.source_refs,
        signal_evidence=selected_signals,
        source_manifest=narrowed_manifest,
        pipeline_failures=pipeline_failures,
        provenance=ImprovementReviewProvenance(
            retro_id=context.retro_id,
            candidate_id=candidate.candidate_id,
            context_sha256=context_digest,
            candidate_batch_digest=batch_digest,
        ),
    )
    data = canonical_json_bytes(subject)
    return subject, sha256_bytes(data), data


def publish_review_subject(project_root: Path, subject_sha256: str, canonical_bytes: bytes) -> Path:
    """Write one content-addressed subject immutably and fail on any conflict."""
    if sha256_bytes(canonical_bytes) != subject_sha256:
        raise ImprovementReviewSubjectError("subject bytes do not match subject_sha256")
    path = project_root / "qa" / "improvements" / "review-subjects" / f"{subject_sha256}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_bytes() != canonical_bytes:
            raise ImprovementReviewSubjectError(f"review subject conflict: {subject_sha256}")
        return path
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(canonical_bytes)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return path


__all__ = [
    "ImprovementReviewSubjectError",
    "build_review_subject",
    "publish_review_subject",
]
