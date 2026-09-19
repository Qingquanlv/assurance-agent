"""Pure obligation normalization and gap calculation. No file I/O or auth."""

from __future__ import annotations

from collections.abc import Mapping

from assurance_intake.contracts.explore import ObligationDraftV1, SourceQuoteV1
from assurance_intake.contracts.obligations import (
    DiscoveryAuditRowV1,
    ExpectedBasisV1,
    PreparedObligationV1,
    RequiredObservationV1,
    SourceRefV1,
    VerificationRequirementV1,
)

_MRC_PREFIX = {
    "api": "API",
    "e2e": "E2E",
    "e2e_if_enabled": "E2E",
    "negative": "NEGATIVE",
    "data_integrity": "DATA-INTEGRITY",
}


class InputError(ValueError):
    """Raised for dangling or mistyped obligation references."""


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


def _resolved_refs(
    quotes: tuple[SourceQuoteV1, ...],
    resolved_quotes: Mapping[tuple[str, str], SourceRefV1],
) -> tuple[SourceRefV1, ...]:
    refs: list[SourceRefV1] = []
    seen: set[tuple[str, str]] = set()
    for quote in quotes:
        key = (quote.source_id, quote.quote)
        ref = resolved_quotes.get(key)
        if ref is None or key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return tuple(refs)


def normalize_obligation_drafts(
    drafts: tuple[ObligationDraftV1, ...],
    *,
    resolved_quotes: Mapping[tuple[str, str], SourceRefV1],
) -> tuple[PreparedObligationV1, ...]:
    rows: list[PreparedObligationV1] = []
    for sequence, draft in enumerate(drafts, start=1):
        mrc_id = (
            draft.draft_id
            if draft.draft_id.startswith("MRC-")
            else f"MRC-{_MRC_PREFIX[draft.category]}-{sequence:03d}"
        )
        observations = tuple(
            RequiredObservationV1(
                observation_key=goal.key,
                condition=goal.condition,
                predicate="status_code_eq",
                expected=goal.proposed_expected_status,
                basis_refs=_resolved_refs(goal.basis_quotes, resolved_quotes),
            )
            for goal in draft.observation_goals
        )
        if draft.proposed_profile_id is None and not observations:
            requirements: tuple[VerificationRequirementV1, ...] = ()
        else:
            requirements = (
                VerificationRequirementV1(
                    requirement_id=f"{mrc_id}-R001",
                    profile_id=draft.proposed_profile_id or "method_unspecified",
                    prerequisites=draft.prerequisites,
                    observations=observations,
                    semantic_review_required=True,
                    subject_binding_required=True,
                ),
            )
        rows.append(
            PreparedObligationV1(
                mrc_id=mrc_id,
                key=None,
                proposed_key=draft.proposed_key,
                category=draft.category,
                layer=draft.layer,
                statement=draft.statement,
                applicability_conditions=draft.applicability_conditions,
                expected_basis_refs=tuple(
                    ExpectedBasisV1(source=ref, source_status="pending")
                    for ref in _resolved_refs(draft.basis_quotes, resolved_quotes)
                ),
                impact_row_ids=draft.impact_row_ids,
                required=True,
                scope_disposition="included",
                exclusion_basis=None,
                open_questions=draft.open_questions,
                verification_requirements=requirements,
            )
        )
    ids = [row.mrc_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate MRC id")
    return tuple(sorted(rows, key=lambda row: (row.mrc_id, row.proposed_key or "", row.key or "")))


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
    explained = {
        entry.source.locator
        for entry in audit
        if entry.disposition in {"excluded", "pending"}
    }
    gaps: set[str] = set()
    for row_id in impact_rows:
        if row_id not in covered and row_id not in explained:
            gaps.add("impact_unmapped")
    return tuple(sorted(gaps))
