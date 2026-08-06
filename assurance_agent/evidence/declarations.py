"""Lane C declaration proposal builder, writer, and intake adapter.

Deterministic templates when obligation/declaration is missing — distinct from
Lane B ``test_improvement`` delivery. Zero LLM. Not an M4 metrics aggregator.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.coverage_gaps import (
    CoverageGap,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.declarations import (
    DECLARATION_PROPOSAL_DIR_REL,
    DeclarationCaseDraft,
    DeclarationCaseLayer,
    DeclarationEvidenceRef,
    DeclarationProposal,
    DeclarationProposalReceipt,
)

_CASE_ID_SAFE = re.compile(r"[^A-Za-z0-9_]+")


@dataclass(frozen=True)
class DeclarationIntakeVerdict:
    """Typed intake-evidence check result (not a CLI run)."""

    ok: bool
    errors: tuple[str, ...]


def declaration_proposal_relpath(improvement_id: str) -> str:
    return f"{DECLARATION_PROPOSAL_DIR_REL}/{improvement_id}.proposal.yaml"


def _gap_locator_string(gap: CoverageGap) -> str:
    loc = gap.locator
    parts = [f"kind={gap.kind}"]
    if loc.case_id:
        parts.append(f"case_id={loc.case_id}")
    if loc.constraint_key:
        parts.append(f"constraint_key={loc.constraint_key}")
    if loc.cell:
        parts.append(f"cell={loc.cell}")
    if loc.cluster_key:
        parts.append(f"cluster_key={loc.cluster_key}")
    return ";".join(parts)


def _sanitize_case_token(raw: str) -> str:
    cleaned = _CASE_ID_SAFE.sub("_", raw).strip("_")
    return cleaned.upper() if cleaned else "UNKNOWN"


def _case_id_for_gap(gap: CoverageGap) -> str:
    loc = gap.locator
    if loc.case_id:
        return loc.case_id
    if loc.constraint_key:
        return f"TC_DECL_{_sanitize_case_token(loc.constraint_key)}"
    if loc.cell:
        return f"TC_DECL_{_sanitize_case_token(loc.cell)}"
    if loc.cluster_key:
        return f"TC_DECL_{_sanitize_case_token(loc.cluster_key)}"
    return f"TC_DECL_{_sanitize_case_token(gap.kind)}"


def _layer_for_gap(gap: CoverageGap) -> DeclarationCaseLayer:
    kind = gap.kind
    if "matrix" in kind or kind.startswith("unmapped"):
        return "api"
    if "constraint" in kind:
        return "fuzz"
    return "api"


def _title_for_gap(gap: CoverageGap) -> str:
    loc = gap.locator
    subject = loc.constraint_key or loc.case_id or loc.cell or loc.cluster_key or gap.kind
    return f"Declare obligation for {gap.kind}: {subject}"


def _fingerprint_for_parts(parts: Sequence[tuple[str, str]]) -> str:
    payload = [{"kind": kind, "locator": locator} for kind, locator in parts]
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _improvement_id_for_fingerprint(fingerprint: str) -> str:
    return f"IMP-{fingerprint[:20].upper()}"


def _declaration_gaps(gaps: Sequence[CoverageGap]) -> tuple[CoverageGap, ...]:
    return tuple(gap for gap in gaps if gap.layer == "declaration")


def build_declaration_proposal_from_gaps(
    document: CoverageGapsDocument,
    *,
    source_improvement_id: str | None = None,
) -> DeclarationProposal:
    """Template a Lane C proposal from declaration-layer coverage gaps.

    Execution-layer gaps are ignored (Lane B / ``test_improvement`` path).
    Raises when no declaration-layer gap is present.
    """
    selected = _declaration_gaps(document.gaps)
    if not selected:
        raise ValueError("declaration proposal requires at least one declaration-layer coverage gap")

    case_drafts: list[DeclarationCaseDraft] = []
    evidence_refs: list[DeclarationEvidenceRef] = []
    fp_parts: list[tuple[str, str]] = []

    for gap in selected:
        locator = _gap_locator_string(gap)
        digest = gap.evidence_refs[0] if gap.evidence_refs else document.projection_digest
        case_drafts.append(
            DeclarationCaseDraft(
                case_id=_case_id_for_gap(gap),
                title=_title_for_gap(gap),
                layer=_layer_for_gap(gap),
                status="draft",
            )
        )
        evidence_refs.append(
            DeclarationEvidenceRef(
                kind="coverage_gap",
                locator=locator,
                digest=digest,
            )
        )
        fp_parts.append(("coverage_gap", locator))

    # Deterministic order for case drafts / evidence (already gap-doc ordered).
    fingerprint = _fingerprint_for_parts(fp_parts)
    return DeclarationProposal(
        schema_version="1",
        improvement_id=_improvement_id_for_fingerprint(fingerprint),
        change_id=document.change_id,
        status="draft",
        case_drafts=tuple(case_drafts),
        evidence_refs=tuple(evidence_refs),
        source_improvement_id=source_improvement_id,
        fingerprint=fingerprint,
    )


def build_declaration_proposal_from_counterexample(
    *,
    counterexample_id: str,
    digest: str,
    change_id: str | None = None,
    path: str | None = None,
    source_improvement_id: str | None = None,
    layer: DeclarationCaseLayer = "api",
) -> DeclarationProposal:
    """Template a Lane C proposal from a counterexample identity + digest.

    Escape/CE content is never copied — only ``counterexample_id`` + digest.
    """
    if not counterexample_id.strip():
        raise ValueError("counterexample_id must be non-empty")
    if not digest.strip():
        raise ValueError("counterexample digest must be non-empty")

    locator = f"counterexample_id={counterexample_id}"
    token = _sanitize_case_token(counterexample_id)
    fingerprint = _fingerprint_for_parts((("counterexample", locator),))
    return DeclarationProposal(
        schema_version="1",
        improvement_id=_improvement_id_for_fingerprint(fingerprint),
        change_id=change_id,
        status="draft",
        case_drafts=(
            DeclarationCaseDraft(
                case_id=f"TC_DECL_{token}",
                title=f"Declare obligation from counterexample {counterexample_id}",
                layer=layer,
                status="draft",
            ),
        ),
        evidence_refs=(
            DeclarationEvidenceRef(
                kind="counterexample",
                locator=locator,
                digest=digest,
                path=path,
            ),
        ),
        source_improvement_id=source_improvement_id,
        fingerprint=fingerprint,
    )


def write_declaration_proposal(project_root: Path, proposal: DeclarationProposal) -> Path:
    """Write ``qa/improvements/declarations/<improvement-id>.proposal.yaml``."""
    rel = declaration_proposal_relpath(proposal.improvement_id)
    path = project_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = proposal.model_dump(mode="json")
    text = yaml.safe_dump(payload, sort_keys=True, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_declaration_proposal_receipt(
    project_root: Path,
    proposal: DeclarationProposal,
) -> DeclarationProposalReceipt:
    """Write the proposal and return a thin digest receipt."""
    path = write_declaration_proposal(project_root, proposal)
    digest = sha256_bytes(canonical_json_bytes(proposal))
    return DeclarationProposalReceipt(
        improvement_id=proposal.improvement_id,
        path=path.relative_to(project_root).as_posix(),
        digest=digest,
        status=proposal.status,
    )


def validate_declaration_intake(
    proposal: DeclarationProposal | Mapping[str, Any],
) -> DeclarationIntakeVerdict:
    """Check that a declaration proposal can serve as intake requirement evidence.

    Peer to ``proposal.md``: required fields present, digests non-empty, every
    case entry ``draft``. Does not run the intake CLI.
    """
    errors: list[str] = []
    if isinstance(proposal, DeclarationProposal):
        data = proposal.model_dump(mode="json")
    else:
        data = dict(proposal)

    for key in ("schema_version", "improvement_id", "status", "fingerprint"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"missing or empty required field: {key}")

    case_drafts = data.get("case_drafts")
    if not isinstance(case_drafts, (list, tuple)) or not case_drafts:
        errors.append("case_drafts must be a non-empty list")
    else:
        for index, entry in enumerate(case_drafts):
            if not isinstance(entry, Mapping):
                errors.append(f"case_drafts[{index}] must be an object")
                continue
            status = entry.get("status")
            if status != "draft":
                errors.append(
                    f"case_drafts[{index}] status must be draft for intake "
                    f"(got {status!r}; active/non-draft cases require case review gate)"
                )
            for field in ("case_id", "title", "layer"):
                field_value = entry.get(field)
                if not isinstance(field_value, str) or not field_value.strip():
                    errors.append(f"case_drafts[{index}].{field} must be non-empty")

    evidence_refs = data.get("evidence_refs")
    if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
        errors.append("evidence_refs must be a non-empty list")
    else:
        for index, ref in enumerate(evidence_refs):
            if not isinstance(ref, Mapping):
                errors.append(f"evidence_refs[{index}] must be an object")
                continue
            kind = ref.get("kind")
            if kind not in {
                "counterexample",
                "escape",
                "mutation_survivor",
                "coverage_gap",
            }:
                errors.append(f"evidence_refs[{index}].kind is invalid: {kind!r}")
            locator = ref.get("locator")
            if not isinstance(locator, str) or not locator.strip():
                errors.append(f"evidence_refs[{index}].locator must be non-empty")
            digest = ref.get("digest")
            if not isinstance(digest, str) or not digest.strip():
                errors.append(f"evidence_refs[{index}].digest must be non-empty")

    return DeclarationIntakeVerdict(ok=not errors, errors=tuple(errors))


__all__ = [
    "DeclarationIntakeVerdict",
    "build_declaration_proposal_from_counterexample",
    "build_declaration_proposal_from_gaps",
    "declaration_proposal_relpath",
    "validate_declaration_intake",
    "write_declaration_proposal",
    "write_declaration_proposal_receipt",
]
