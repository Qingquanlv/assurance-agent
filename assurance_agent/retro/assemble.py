"""Mechanical assembly of validated Retro v3 domain signals."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.retro_v3 import (
    ContextSignalSet,
    CoverageGapEvidenceSlice,
    DiscoveryEvidenceSlice,
    DomainAnalysisStatus,
    DomainStatuses,
    EvalEvidenceSlice,
    ImprovementCandidateDocumentV3,
    IssueEvidenceSlice,
    RetroContextV3,
    RetroIntegrity,
    RetroSourceManifestV3,
    RetroWindow,
    SignalDocumentV3,
    WorkflowEvidenceSlice,
)
from assurance_agent.exceptions import AaError


class RetroAssembleError(AaError):
    """Required v3 assembly input is absent or schema-invalid."""


def _load_json(path: Path) -> tuple[dict, bytes]:
    try:
        data = path.read_bytes()
        payload = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetroAssembleError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RetroAssembleError(f"{path} must contain a JSON object")
    return payload, data


def assemble_context(retro_dir: Path, *, dry_run: bool, now: datetime) -> RetroContextV3:
    """Verify core and present optional signal/slice pairs without filtering."""
    try:
        window_raw, _ = _load_json(retro_dir / "window.json")
        window = RetroWindow.model_validate(window_raw)
    except ValidationError as exc:
        raise RetroAssembleError(f"invalid Retro window: {exc}") from exc

    slice_types = {
        "issue": IssueEvidenceSlice,
        "workflow": WorkflowEvidenceSlice,
        "eval": EvalEvidenceSlice,
    }
    optional_slice_types = {
        "discovery": DiscoveryEvidenceSlice,
        "coverage_gap": CoverageGapEvidenceSlice,
    }
    for domain, slice_type in optional_slice_types.items():
        slice_path = retro_dir / "evidence" / f"{domain}-slice.json"
        signal_path = retro_dir / "signals" / f"{domain}.json"
        if slice_path.is_file() != signal_path.is_file():
            raise RetroAssembleError(f"{domain} slice/signal pair is incomplete")
        if slice_path.is_file():
            slice_types[domain] = slice_type  # type: ignore[assignment]
    slices = {}
    slice_digests: dict[str, str] = {}
    statuses: dict[str, DomainAnalysisStatus] = {}
    signals: dict[str, tuple] = {}
    reasons: list[str] = []

    for domain, slice_type in slice_types.items():
        slice_raw, slice_bytes = _load_json(retro_dir / "evidence" / f"{domain}-slice.json")
        signal_raw, _ = _load_json(retro_dir / "signals" / f"{domain}.json")
        try:
            slice_ = slice_type.model_validate(slice_raw)
            signal_doc = SignalDocumentV3.model_validate(signal_raw)
        except ValidationError as exc:
            raise RetroAssembleError(f"invalid {domain} assembly input: {exc}") from exc
        if slice_.window != window or slice_.retro_id != signal_doc.retro_id:
            raise RetroAssembleError(f"{domain} assembly identity mismatch")
        slices[domain] = slice_
        actual_digest = sha256_bytes(slice_bytes)
        slice_digests[domain] = actual_digest
        for reason in slice_.integrity.reasons:
            if reason not in reasons:
                reasons.append(reason)
        if signal_doc.slice_sha256 != actual_digest:
            statuses[domain] = DomainAnalysisStatus(status="failed", failure_reason="slice_digest_mismatch")
            signals[domain] = slice_.deterministic_signals
            reasons.append(f"{domain}_signal_analysis_failed")
        elif signal_doc.analysis_status == "failed":
            statuses[domain] = DomainAnalysisStatus(status="failed", failure_reason=signal_doc.failure_reason)
            signals[domain] = slice_.deterministic_signals
            reasons.append(f"{domain}_signal_analysis_failed")
        else:
            statuses[domain] = DomainAnalysisStatus(status="ok")
            signals[domain] = (*slice_.deterministic_signals, *signal_doc.signals)

    integrity = (
        RetroIntegrity(status="incomplete", reasons=tuple(dict.fromkeys(reasons)))
        if reasons
        else RetroIntegrity(status="complete")
    )
    signal_set = ContextSignalSet(
        issue=signals["issue"],
        workflow=signals["workflow"],
        eval=signals["eval"],
        discovery=signals.get("discovery", ()),
        coverage_gap=signals.get("coverage_gap", ()),
    )
    discovery_slice = slices.get("discovery")
    coverage_gap_slice = slices.get("coverage_gap")
    return RetroContextV3(
        retro_id=next(iter(slices.values())).retro_id,
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        dry_run=dry_run,
        window=window,
        source_manifest=RetroSourceManifestV3(
            issue_slice_sha256=slice_digests["issue"],
            workflow_slice_sha256=slice_digests["workflow"],
            eval_slice_sha256=slice_digests["eval"],
            discovery_slice_sha256=slice_digests.get("discovery"),
            coverage_gap_slice_sha256=slice_digests.get("coverage_gap"),
            issue_sources=slices["issue"].sources,
            workflow_sources=slices["workflow"].sources,
            eval_sources=slices["eval"].sources,
            discovery_sources=discovery_slice.sources if discovery_slice is not None else (),
            coverage_gap_sources=(coverage_gap_slice.sources if coverage_gap_slice is not None else ()),
        ),
        integrity=integrity,
        domain_status=DomainStatuses(
            issue=statuses["issue"],
            workflow=statuses["workflow"],
            eval=statuses["eval"],
            discovery=statuses.get("discovery"),
            coverage_gap=statuses.get("coverage_gap"),
        ),
        signals=signal_set,
        signal_count=sum(len(items) for items in signals.values()),
    )


def write_noop_receipt(retro_dir: Path, context: RetroContextV3) -> None:
    """Write the deterministic v3 empty Candidate receipt and summary."""
    if context.signal_count != 0:
        raise RetroAssembleError("NOOP receipt requires signal_count == 0")
    context_digest = sha256_bytes(canonical_json_bytes(context))
    candidate_doc = ImprovementCandidateDocumentV3(
        retro_id=context.retro_id,
        context_sha256=context_digest,
        candidates=(),
    )
    status_by_domain = {
        "issue": context.domain_status.issue,
        "workflow": context.domain_status.workflow,
        "eval": context.domain_status.eval,
        "discovery": context.domain_status.discovery,
        "coverage_gap": context.domain_status.coverage_gap,
    }
    failed = [
        name for name, status in status_by_domain.items() if status is not None and status.status == "failed"
    ]
    reason = "analysis_incomplete" if failed else "no_actionable_signals"
    summary = f"# Retro {context.retro_id}\n\nresult: {reason}\n"
    if failed:
        summary += f"failed_domains: {', '.join(failed)}\n"
    (retro_dir / "proposal-candidates.json").write_bytes(canonical_json_bytes(candidate_doc))
    (retro_dir / "retro-summary.md").write_text(summary, encoding="utf-8")


__all__ = [
    "RetroAssembleError",
    "assemble_context",
    "sha256_bytes",
    "write_noop_receipt",
]
