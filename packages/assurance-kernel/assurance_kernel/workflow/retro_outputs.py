"""Complete and validate agent-authored Retro v3 outputs before freeze."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.artifacts.models.retro_v3 import (
    EvalEvidenceSlice,
    ImprovementCandidateDocumentDraftV3,
    ImprovementCandidateDocumentV3,
    IssueEvidenceSlice,
    RetroContextV3,
    SignalDocumentV3,
    SignalDraftDocument,
    WorkflowEvidenceSlice,
)

EvidenceSlice = IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice

_SIGNAL_OUTPUT_RE = re.compile(
    r"^project:qa/retro/(?P<retro_id>[^/]+)/signals/(?P<domain>issue|workflow|eval)\.json$"
)
_CANDIDATE_OUTPUT_RE = re.compile(r"^project:qa/retro/(?P<retro_id>[^/]+)/proposal-candidates\.json$")
_REF_FIELDS = {
    "issue": frozenset({"problem_ids", "occurrence_ids", "issue_event_ids"}),
    "workflow": frozenset({"workflow_evidence_ids"}),
    "eval": frozenset({"eval_run_ids"}),
}
_SIGNAL_TYPES = {
    # batch_member_evidence_gap is a first-class Retro v3 signal (see
    # BatchMemberEvidenceGapSignal) emitted as deterministic evidence when a
    # batch member lacks a ledger/projection. Analyzers may re-surface it in
    # domain signal drafts; rejecting it here forced invalid_output retries and
    # collapsed propose into mechanical recovery fallbacks.
    "issue": frozenset({"issue_pattern", "batch_member_evidence_gap"}),
    "workflow": frozenset(
        {"gate_pushback", "task_failure", "healing", "skill_drift", "batch_member_evidence_gap"}
    ),
    "eval": frozenset({"eval_trend", "batch_member_evidence_gap"}),
}
_SLICE_MODELS = {
    "issue": IssueEvidenceSlice,
    "workflow": WorkflowEvidenceSlice,
    "eval": EvalEvidenceSlice,
}


class SignalInvalidError(ValueError):
    """A signal draft cannot be safely completed and frozen."""


class CandidateOutputError(ValueError):
    """The proposer output cannot be safely completed."""


def _normalize_signal_draft_doc(doc: Mapping[str, object]) -> dict[str, object]:
    """Tolerate common agent shapes for deterministic batch-gap signals.

    ``materialize_slices`` always cites gap signals via ``workflow_evidence_ids``
    (self-id), including on issue/eval slices. Agents that mirror that shape — or
    omit refs entirely — used to fail closed before analysis could settle.
    """
    payload = dict(doc)
    signals = payload.get("signals")
    if not isinstance(signals, list):
        return payload
    normalized: list[object] = []
    for item in signals:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        signal = dict(item)
        if signal.get("signal_type") == "batch_member_evidence_gap":
            refs_raw = signal.get("source_refs")
            refs = dict(refs_raw) if isinstance(refs_raw, dict) else {}
            if not any(
                refs.get(key)
                for key in (
                    "problem_ids",
                    "occurrence_ids",
                    "issue_event_ids",
                    "workflow_evidence_ids",
                    "eval_run_ids",
                )
            ):
                signal_id = signal.get("signal_id")
                if isinstance(signal_id, str) and signal_id.strip():
                    refs["workflow_evidence_ids"] = [signal_id]
            signal["source_refs"] = refs
        normalized.append(signal)
    payload["signals"] = normalized
    return payload


def _normalize_candidate_draft_doc(doc: Mapping[str, object]) -> dict[str, object]:
    """Coerce common proposer mistakes before schema validation."""
    payload = dict(doc)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return payload
    normalized: list[object] = []
    for item in candidates:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        candidate = dict(item)
        verification = candidate.get("verification")
        if isinstance(verification, dict):
            verification = dict(verification)
            criteria = verification.get("success_criteria")
            if isinstance(criteria, list):
                verification["success_criteria"] = " ".join(
                    str(part).strip() for part in criteria if str(part).strip()
                )
            candidate["verification"] = verification
        normalized.append(candidate)
    payload["candidates"] = normalized
    return payload


def validate_signal_draft(
    domain: str,
    doc: Mapping[str, object],
    slice_: EvidenceSlice,
) -> SignalDraftDocument:
    """Validate schema, run/domain identity, and immutable evidence references."""
    try:
        draft = SignalDraftDocument.model_validate(_normalize_signal_draft_doc(doc))
    except ValidationError as exc:
        raise SignalInvalidError(f"invalid {domain} signal draft: {exc}") from exc
    if domain not in _REF_FIELDS or draft.domain != domain:
        raise SignalInvalidError(f"signal domain mismatch: output={domain}, document={draft.domain}")
    if draft.retro_id != slice_.retro_id:
        raise SignalInvalidError(
            f"signal retro_id mismatch: document={draft.retro_id}, slice={slice_.retro_id}"
        )
    resolvable = slice_.resolvable_ids()
    allowed_fields = _REF_FIELDS[domain]
    seen_signal_ids: set[str] = set()
    for signal in draft.signals:
        if signal.signal_id in seen_signal_ids:
            raise SignalInvalidError(f"duplicate signal_id {signal.signal_id!r} in {domain} signal draft")
        seen_signal_ids.add(signal.signal_id)
        if signal.signal_type not in _SIGNAL_TYPES[domain]:
            raise SignalInvalidError(
                f"{domain} signal {signal.signal_id} has illegal signal_type: {signal.signal_type}"
            )
        refs = signal.source_refs.model_dump()
        populated_fields = {name for name, values in refs.items() if values}
        allowed = set(allowed_fields)
        # Deterministic batch gaps are always self-cited on workflow_evidence_ids.
        if signal.signal_type == "batch_member_evidence_gap":
            allowed.add("workflow_evidence_ids")
        disallowed = populated_fields - allowed
        if disallowed:
            raise SignalInvalidError(
                f"{domain} signal {signal.signal_id} uses cross-domain refs: {sorted(disallowed)}"
            )
        unknown = set(signal.source_refs.all_ids()) - resolvable
        if unknown:
            raise SignalInvalidError(
                f"{domain} signal {signal.signal_id} has unknown evidence refs: {sorted(unknown)}"
            )
    return draft


def backfill_slice_digest(doc: SignalDraftDocument, slice_bytes: bytes) -> SignalDocumentV3:
    """Create the canonical model after hashing the exact frozen slice bytes."""
    payload = doc.model_dump(mode="json")
    payload["slice_sha256"] = sha256_bytes(slice_bytes)
    try:
        return SignalDocumentV3.model_validate(payload)
    except ValidationError as exc:
        raise SignalInvalidError(f"completed signal document is invalid: {exc}") from exc


def complete_signal_outputs(project_root: Path, outputs: tuple[str, ...]) -> None:
    """Complete every declared v3 signal output before TreeStore freeze."""
    for output in outputs:
        match = _SIGNAL_OUTPUT_RE.fullmatch(output)
        if match is None:
            continue
        retro_id = match.group("retro_id")
        domain = match.group("domain")
        signal_path = project_root / "qa" / "retro" / retro_id / "signals" / f"{domain}.json"
        slice_path = project_root / "qa" / "retro" / retro_id / "evidence" / f"{domain}-slice.json"
        try:
            raw_doc = json.loads(signal_path.read_text(encoding="utf-8"))
            slice_bytes = slice_path.read_bytes()
            raw_slice = json.loads(slice_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignalInvalidError(f"cannot read {domain} signal completion inputs: {exc}") from exc
        if not isinstance(raw_doc, dict) or not isinstance(raw_slice, dict):
            raise SignalInvalidError(f"{domain} signal completion inputs must be JSON objects")
        try:
            slice_ = _SLICE_MODELS[domain].model_validate(raw_slice)
        except ValidationError as exc:
            raise SignalInvalidError(f"invalid {domain} evidence slice: {exc}") from exc
        draft = validate_signal_draft(domain, raw_doc, slice_)
        signal_path.write_bytes(canonical_json_bytes(backfill_slice_digest(draft, slice_bytes)))


def complete_candidate_outputs(project_root: Path, outputs: tuple[str, ...]) -> None:
    """Complete every declared v3 Candidate output before TreeStore freeze."""
    for output in outputs:
        match = _CANDIDATE_OUTPUT_RE.fullmatch(output)
        if match is None:
            continue
        retro_id = match.group("retro_id")
        retro_dir = project_root / "qa" / "retro" / retro_id
        candidate_path = retro_dir / "proposal-candidates.json"
        context_path = retro_dir / "context.json"
        try:
            raw = json.loads(candidate_path.read_text(encoding="utf-8"))
            context_bytes = context_path.read_bytes()
            context_raw = json.loads(context_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateOutputError(f"cannot read Candidate completion inputs: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != "3":
            return
        try:
            draft = ImprovementCandidateDocumentDraftV3.model_validate(_normalize_candidate_draft_doc(raw))
            context = RetroContextV3.model_validate(context_raw)
        except ValidationError as exc:
            raise CandidateOutputError(f"invalid v3 Candidate completion input: {exc}") from exc
        if draft.retro_id != context.retro_id or draft.retro_id != retro_id:
            raise CandidateOutputError("Candidate/context retro_id mismatch")
        payload = draft.model_dump(mode="json")
        payload["context_sha256"] = sha256_bytes(context_bytes)
        try:
            completed = ImprovementCandidateDocumentV3.model_validate(payload)
        except ValidationError as exc:
            raise CandidateOutputError(f"completed v3 Candidate document is invalid: {exc}") from exc
        candidate_path.write_bytes(canonical_json_bytes(completed))


__all__ = [
    "CandidateOutputError",
    "SignalInvalidError",
    "backfill_slice_digest",
    "complete_candidate_outputs",
    "complete_signal_outputs",
    "validate_signal_draft",
]
