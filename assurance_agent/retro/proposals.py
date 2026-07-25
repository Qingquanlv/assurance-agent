from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_path_segment_safe
from assurance_agent.retro.types import (
    FINDING_TO_APPLY,
    DataKnowledgeProposal,
    IssueDraftPayload,
    MemoryBodyPayload,
    RetroContext,
    RetroProposal,
    memory_body_text,
)


def _describe_rejection(index: int, entry: object, err: ValidationError) -> str:
    ident = entry.get("id") if isinstance(entry, dict) else None
    reasons = "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or '<root>'}: {issue['msg']}"
        for issue in err.errors()[:3]
    )
    return f"{ident or f'entry #{index}'} ({reasons})"


def _load_proposals_document(path: Path, *, strict: bool) -> tuple[dict | list | None, list]:
    """Parse ``proposals.json`` into ``(raw_document, entries)``.

    Returns ``(None, [])`` when the file is absent. Invalid JSON raises under
    ``strict`` and returns ``(None, [])`` otherwise.
    """
    if not path.exists():
        return None, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        if strict:
            raise AaError(f"{path} is not valid JSON: {err}") from err
        return None, []
    entries = raw.get("proposals", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        if strict:
            raise AaError(f"{path}: expected a proposals list, got {type(entries).__name__}")
        return raw if isinstance(raw, (dict, list)) else None, []
    return raw if isinstance(raw, (dict, list)) else {"proposals": entries}, entries


def _parse_proposal_entries(
    entries: list, *, path: Path, strict: bool
) -> list[RetroProposal]:
    proposals: list[RetroProposal] = []
    rejected: list[str] = []
    for index, entry in enumerate(entries):
        try:
            proposals.append(RetroProposal.model_validate(entry))
        except ValidationError as err:
            rejected.append(_describe_rejection(index, entry, err))
    if rejected and strict:
        raise AaError(
            f"{path}: {len(rejected)} of {len(entries)} proposals are unroutable and were "
            f"rejected: {', '.join(rejected)}"
        )
    return proposals


def read_proposals(retro_dir: Path, *, strict: bool = True) -> list[RetroProposal]:
    """Read ``proposals.json``, rejecting entries that cannot be routed.

    Unparseable entries used to be dropped silently, which let a whole retro run
    disappear from every consumer — promote, apply, export and the nightly
    partition all saw an empty list while the file on disk looked populated.
    Strict mode (the default, used by every acting path) raises instead. Callers
    that merely survey historical runs pass ``strict=False`` so that one legacy
    directory cannot take down a global listing.
    """
    path = retro_dir / "proposals.json"
    _raw, entries = _load_proposals_document(path, strict=strict)
    if not entries:
        return []
    return _parse_proposal_entries(entries, path=path, strict=strict)


def write_proposals_document(
    retro_dir: Path,
    proposals: list[RetroProposal],
    *,
    envelope: dict | None = None,
) -> Path:
    """Persist proposals in the canonical three-track shape (finding_kind + payload)."""
    path = retro_dir / "proposals.json"
    retro_dir.mkdir(parents=True, exist_ok=True)
    doc: dict = dict(envelope or {})
    doc["proposals"] = [proposal.model_dump(mode="json") for proposal in proposals]
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def accept_proposals(retro_dir: Path) -> list[RetroProposal]:
    """Write-boundary gate: validate agent output and rewrite the canonical shape.

    Nightly collect calls this immediately after the proposal agent exits. Legacy
    prose-only entries (``apply_kind`` + ``proposed_change``, no ``finding_kind`` /
    ``payload``) are adapted once via the RetroProposal shim and persisted with
    machine fields filled in, so later consumers do not depend on the shim.
    Unroutable entries fail closed — the same role ``invalid_output`` plays for
    graph must_compat artifacts, without binding proposals.json into the ingest
    catalog.
    """
    path = retro_dir / "proposals.json"
    if not path.exists():
        raise AaError(f"proposals.json missing after agent run: {path}")
    raw, entries = _load_proposals_document(path, strict=True)
    proposals = _parse_proposal_entries(entries, path=path, strict=True)
    envelope = raw if isinstance(raw, dict) else {}
    envelope = {k: v for k, v in envelope.items() if k != "proposals"}
    write_proposals_document(retro_dir, proposals, envelope=envelope)
    return proposals


def _context_evidence_ids(context: RetroContext) -> set[str]:
    """All citable evidence ids the aggregator emitted into this context."""
    signals = context.signals
    ids: set[str] = set()
    for signal in signals.failure_distribution:
        ids.update(signal.evidence_ids)
    for signal in signals.gate_pushback:
        ids.update(signal.evidence_ids)
    ids.update(signals.healing_efficiency.evidence_ids)
    for signal in signals.reclassifications:
        ids.update(signal.evidence_ids)
    for decision in signals.human_decisions:
        if decision.evidence_id:
            ids.add(decision.evidence_id)
    for signal in signals.skill_execution:
        ids.update(signal.evidence_ids)
    return ids


def validate_retro_proposals(context: RetroContext, proposals: list[RetroProposal]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    valid_evidence = _context_evidence_ids(context)
    for proposal in proposals:
        if proposal.id in seen:
            errors.append(f"duplicate proposal id: {proposal.id}")
        seen.add(proposal.id)
        try:
            assert_path_segment_safe(proposal.id, label="proposal id")
        except UnsafeIdentifierError as err:
            errors.append(str(err))
        expected_apply = FINDING_TO_APPLY[proposal.finding_kind]
        if proposal.apply_kind != expected_apply:
            errors.append(
                f"proposal {proposal.id}: finding_kind={proposal.finding_kind!r} "
                f"requires apply_kind={expected_apply!r}"
            )
        if proposal.apply_kind == "memory_append" and not memory_body_text(proposal):
            errors.append(f"memory_append proposal {proposal.id} has empty body")
        if proposal.apply_kind == "issue_export" and not isinstance(proposal.payload, IssueDraftPayload):
            errors.append(f"issue_export proposal {proposal.id} missing IssueDraftPayload")
        if proposal.apply_kind == "knowledge_delta":
            if not isinstance(proposal.payload, DataKnowledgeProposal):
                errors.append(f"knowledge_delta proposal {proposal.id} missing DataKnowledgeProposal")
            elif proposal.payload.mode != "delta":
                errors.append(f"knowledge_delta proposal {proposal.id} must have mode: delta")
        if not proposal.evidence_ids:
            errors.append(f"proposal {proposal.id} cites no evidence_ids")
            continue
        unknown = [eid for eid in proposal.evidence_ids if eid not in valid_evidence]
        if unknown:
            errors.append(
                f"proposal {proposal.id} cites evidence_ids absent from context: {sorted(unknown)}"
            )
    return errors
