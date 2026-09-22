"""Obligation normalization, source authentication, and explicit gap calculation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from assurance_intake.contracts.agent import TrustedIntakeSourcesV1
from assurance_intake.contracts.obligations import (
    DiscoveryAuditRowV1,
    PreparedObligationV1,
    SourceRefV1,
)
from assurance_intake.contracts.quality_goals import (
    normalize_goal_obligations,
    normalize_obligation_drafts,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


class InputError(ValueError):
    """Raised for dangling or mistyped obligation references."""


_AUTH_PURPOSE = Literal["expected_basis", "scope_exclusion", "analysis"]
_PURPOSE_KINDS: Mapping[str, frozenset[str]] = {
    "expected_basis": frozenset({"requirement"}),
    "scope_exclusion": frozenset({"requirement", "decision"}),
    "analysis": frozenset({"requirement", "code", "case", "factory", "issue", "risk_check"}),
}


def resolve_requirement_quote(text: str, quote: str, context_quote: str | None = None) -> tuple[int, int]:
    haystack = text
    offset = 0
    if context_quote is not None:
        context_starts = _all_starts(haystack, context_quote)
        if not context_starts:
            raise ValueError("context quote not found")
        if len(context_starts) > 1:
            raise ValueError("ambiguous")
        offset = context_starts[0]
        haystack = haystack[offset : offset + len(context_quote)]
    starts = _all_starts(haystack, quote)
    if not starts:
        raise ValueError("quote not found")
    if len(starts) > 1:
        raise ValueError("ambiguous")
    start = offset + starts[0]
    start_bytes = len(text[:start].encode("utf-8"))
    return start_bytes, start_bytes + len(quote.encode("utf-8"))


def _all_starts(text: str, needle: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while True:
        found = text.find(needle, cursor)
        if found < 0:
            return starts
        starts.append(found)
        cursor = found + 1


def scope_exclusion_allowed(
    *,
    obligation_families: frozenset[str],
    candidate_families: frozenset[str],
    policy_required_families: frozenset[str],
) -> bool:
    return bool(
        obligation_families
        and candidate_families
        and not obligation_families.intersection(candidate_families)
        and not obligation_families.intersection(policy_required_families)
    )


def _read_trusted_bytes(workspace: Path, ref: EvidenceArtifactRefV1) -> bytes:
    path = workspace.joinpath(*ref.path.split("/"))
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"trusted source is missing: {ref.path}")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.digest:
        raise ValueError(f"trusted source digest does not match: {ref.path}")
    return data


def build_source_index(
    sources: TrustedIntakeSourcesV1, *, workspace: Path
) -> Mapping[str, tuple[SourceRefV1, ...]]:
    _read_trusted_bytes(workspace, sources.requirement_ref)
    _read_trusted_bytes(workspace, sources.run_spec_ref)
    return {
        "requirement": (
            SourceRefV1(
                kind="requirement",
                artifact=sources.requirement_ref,
                locator="bytes:0-0",
            ),
        ),
        "run-spec": (
            SourceRefV1(
                kind="decision",
                artifact=sources.run_spec_ref,
                locator="/candidate_test_families",
            ),
        ),
    }


def authenticate_source(
    ref: SourceRefV1,
    *,
    purpose: _AUTH_PURPOSE,
    workspace: Path,
    sources: TrustedIntakeSourcesV1,
) -> None:
    allowed = _PURPOSE_KINDS[purpose]
    if ref.kind not in allowed:
        raise ValueError(f"source kind {ref.kind} is not authorized for {purpose}")
    if purpose == "expected_basis" and ref.kind == "decision":
        raise ValueError("run-spec may not authorize expected_basis")
    if ref.kind == "case":
        raise ValueError("case id is not source evidence")
    if ref.kind == "factory" and not ref.locator.startswith("capabilities."):
        raise ValueError("ordinary leaf cannot be used as factory")
    if ref.kind == "decision" and ref.locator.startswith("ISSUE-"):
        raise ValueError("issue id cannot be used as a decision")
    trusted = {
        sources.requirement_ref.path: sources.requirement_ref.digest,
        sources.run_spec_ref.path: sources.run_spec_ref.digest,
    }
    expected = trusted.get(ref.artifact.path)
    if expected is None or expected != ref.artifact.digest:
        raise ValueError("source artifact is not in the trusted intake index")
    _read_trusted_bytes(workspace, ref.artifact)
    if ref.kind == "decision" and ref.locator != "/candidate_test_families":
        raise ValueError("run-spec locator must be /candidate_test_families")


def apply_scope_exclusions(
    rows: tuple[PreparedObligationV1, ...],
    *,
    candidate_families: frozenset[str],
    policy_required_families: frozenset[str],
    exclusion_basis: SourceRefV1,
) -> tuple[PreparedObligationV1, ...]:
    updated: list[PreparedObligationV1] = []
    for row in rows:
        layers = frozenset({"api", "e2e"} if row.layer == "both" else {row.layer})
        if scope_exclusion_allowed(
            obligation_families=layers,
            candidate_families=candidate_families,
            policy_required_families=policy_required_families,
        ):
            updated.append(
                row.model_copy(
                    update={
                        "required": False,
                        "scope_disposition": "excluded",
                        "exclusion_basis": exclusion_basis,
                    }
                )
            )
        else:
            updated.append(row)
    return tuple(updated)


def obligation_gaps(
    row: PreparedObligationV1,
    *,
    admissible_families: frozenset[str],
    supported_profiles: frozenset[str],
) -> tuple[str, ...]:
    gaps: set[str] = set()
    layers = {"api", "e2e"} if row.layer == "both" else {row.layer}
    if row.required and not layers.intersection(admissible_families):
        gaps.add("family_unavailable")
    if row.key is None:
        gaps.add("capability_unresolved")
    if row.open_questions or not row.expected_basis_refs:
        gaps.add("expectation_unconfirmed")
    if any(basis.source_status != "authenticated" for basis in row.expected_basis_refs):
        gaps.add("expectation_unconfirmed")
    if not row.verification_requirements:
        gaps.add("method_missing")
    for requirement in row.verification_requirements:
        if requirement.profile_id not in supported_profiles:
            gaps.add("method_unsupported")
        if any(item.expected is None for item in requirement.observations):
            gaps.add("expectation_unconfirmed")
    return tuple(sorted(gaps))


def validate_discovery_closure(
    *,
    obligations: tuple[PreparedObligationV1, ...],
    impact_rows: tuple[str, ...],
    audit: tuple[DiscoveryAuditRowV1, ...],
) -> tuple[str, ...]:
    if len(impact_rows) != len(set(impact_rows)):
        raise InputError("duplicate impact row")
    known_rows = set(impact_rows)
    known_mrc = {row.mrc_id for row in obligations}
    for row in obligations:
        unknown = [row_id for row_id in row.impact_row_ids if row_id not in known_rows]
        if unknown:
            raise InputError(f"impact row is not in the current inventory: {unknown}")
    for entry in audit:
        if entry.disposition == "mapped":
            missing = [mrc_id for mrc_id in entry.mrc_ids if mrc_id not in known_mrc]
            if missing:
                raise InputError(f"audit maps unknown MRC: {missing}")
    covered = {row_id for row in obligations for row_id in row.impact_row_ids}
    explained = {entry.source.locator for entry in audit if entry.disposition in {"excluded", "pending"}}
    gaps: set[str] = set()
    for row_id in impact_rows:
        if row_id not in covered and row_id not in explained:
            gaps.add("impact_unmapped")
    return tuple(sorted(gaps))


__all__ = [
    "InputError",
    "apply_scope_exclusions",
    "authenticate_source",
    "build_source_index",
    "normalize_goal_obligations",
    "normalize_obligation_drafts",
    "obligation_gaps",
    "resolve_requirement_quote",
    "scope_exclusion_allowed",
    "validate_discovery_closure",
]
