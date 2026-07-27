"""Complete and validate agent-authored Retro v3 outputs before freeze."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.retro_v3 import (
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
    "issue": frozenset({"issue_pattern"}),
    "workflow": frozenset({"gate_pushback", "task_failure", "healing", "skill_drift"}),
    "eval": frozenset({"eval_trend"}),
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


def validate_signal_draft(
    domain: str,
    doc: Mapping[str, object],
    slice_: EvidenceSlice,
) -> SignalDraftDocument:
    """Validate schema, run/domain identity, and immutable evidence references."""
    try:
        draft = SignalDraftDocument.model_validate(doc)
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
    for signal in draft.signals:
        if signal.signal_type not in _SIGNAL_TYPES[domain]:
            raise SignalInvalidError(
                f"{domain} signal {signal.signal_id} has illegal signal_type: {signal.signal_type}"
            )
        refs = signal.source_refs.model_dump()
        populated_fields = {name for name, values in refs.items() if values}
        disallowed = populated_fields - allowed_fields
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
            draft = ImprovementCandidateDocumentDraftV3.model_validate(raw)
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
