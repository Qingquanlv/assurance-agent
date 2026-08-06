"""MRC matrix + ``report/minimum-coverage-result.json`` (must_compat).

Minimum Required Coverage was historically LLM-authored (matrix) and
LLM-joined against execution (result). Spec
``docs/superpowers/specs/2026-08-03-verification-metrics-evidence-sufficiency-design.md``
§2.2 / §5-A2 / §5-A4 / §12.12 folds both into models and a deterministic
``materialize-minimum-coverage`` operation.

Closed-key discipline (Task 4 handoff):

- ``negative`` / ``data_integrity`` keys must cite
  ``constraint_known_keys(...)`` **or** ``auth.*`` / ``auth_matrix.*``.
- ``e2e_if_enabled`` keys must cite the journey closed set (A4 denominator).
- ``api`` operation names stay free-form (not a DataKnowledge leaf).

Unknown closed-key citations are mechanical findings via
``mrc_closed_key_findings`` — they do not rewrite the join.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, RootModel, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

MrcCategory = Literal["api", "e2e", "e2e_if_enabled", "negative", "data_integrity"]
MrcLayer = Literal["api", "e2e", "both"]
MrcItemStatus = Literal[
    "covered",
    "covered_but_failing",
    "covered_known_issue",
    "not_executed",
    "missing",
    "skipped_by_scope",
]
MrcMatrixRowStatus = Literal["covered", "skipped_by_scope"]
MrcMappingSource = Literal["trace"]
MrcFindingCode = Literal["unknown_closed_key", "mrc_category_unresolved"]

_CONSTRAINT_OR_AUTH_CATEGORIES: frozenset[MrcCategory] = frozenset({"negative", "data_integrity"})
_JOURNEY_CATEGORIES: frozenset[MrcCategory] = frozenset({"e2e_if_enabled", "e2e"})
_KNOWN_CATEGORIES: frozenset[str] = frozenset({"api", "e2e", "e2e_if_enabled", "negative", "data_integrity"})
_CATEGORY_DEFAULT_LAYER: Mapping[MrcCategory, MrcLayer] = {
    "api": "api",
    "negative": "api",
    "data_integrity": "api",
    "e2e": "e2e",
    "e2e_if_enabled": "e2e",
}
_CATEGORY_MRC_PREFIX: Mapping[MrcCategory, str] = {
    "api": "API",
    "e2e": "E2E",
    "e2e_if_enabled": "E2E",
    "negative": "NEGATIVE",
    "data_integrity": "DATA-INTEGRITY",
}


class MinimumCoverageMatrixRow(BaseModel):
    """One row of ``trace/minimum-coverage-matrix.yaml``."""

    model_config = ConfigDict(extra="forbid")

    mrc_id: NonEmptyStr
    key: NonEmptyStr
    required: bool = True
    covered_by_cases: list[str] = []
    status: MrcMatrixRowStatus = "covered"
    skip_reason: str | None = None
    # Optional on legacy matrices; materialize prefers these when present,
    # otherwise resolves from advisory ``minimum_required_coverage`` maps.
    category: MrcCategory | None = None
    layer: MrcLayer | None = None


class MinimumCoverageMatrix(RootModel[list[MinimumCoverageMatrixRow]]):
    """Bare YAML list root used by historical case-design matrices."""


class MrcObligation(BaseModel):
    """Normalized MRC obligation fed to the deterministic join."""

    model_config = _FROZEN

    mrc_id: NonEmptyStr
    key: NonEmptyStr
    category: MrcCategory
    required: bool
    layer: MrcLayer
    case_ids: tuple[str, ...] = ()
    skipped_by_scope: bool = False
    skip_reason: str | None = None
    mapping_source: MrcMappingSource = "trace"


class MinimumCoverageItem(BaseModel):
    model_config = _FROZEN

    mrc_id: NonEmptyStr
    key: NonEmptyStr
    category: MrcCategory
    required: bool
    layer: MrcLayer
    status: MrcItemStatus
    case_ids: tuple[str, ...] = ()
    executed_case_ids: tuple[str, ...] = ()
    mapping_source: MrcMappingSource = "trace"


class MinimumCoverageSummary(BaseModel):
    model_config = _FROZEN

    total_required: int
    covered: int
    covered_known_issue: int
    covered_but_failing: int
    not_executed: int
    missing: int
    skipped_by_scope: int


class MrcFinding(BaseModel):
    """Mechanical check finding (§12.12) — does not rewrite the join."""

    model_config = _FROZEN

    code: MrcFindingCode
    key: NonEmptyStr
    detail: str | None = None


class MinimumCoverageResult(BaseModel):
    """``report/minimum-coverage-result.json`` — MRC × execution join."""

    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    summary: MinimumCoverageSummary
    items: tuple[MinimumCoverageItem, ...]
    # Spec §12.12 closed-key / mapping findings. Default empty for legacy skill JSON.
    findings: tuple[MrcFinding, ...] = ()

    @classmethod
    def of(
        cls,
        *,
        change_id: str,
        items: Sequence[MinimumCoverageItem],
        findings: Sequence[MrcFinding] = (),
    ) -> MinimumCoverageResult:
        summary = summarize_minimum_coverage(items)
        return cls(
            schema_version="1.0",
            change_id=change_id,
            summary=summary,
            items=tuple(items),
            findings=tuple(findings),
        )

    @model_validator(mode="after")
    def _summary_matches_items(self) -> Self:
        expected = summarize_minimum_coverage(self.items)
        if self.summary != expected:
            raise ValueError(
                f"summary {self.summary.model_dump()} disagrees with item tallies {expected.model_dump()}"
            )
        return self


def summarize_minimum_coverage(items: Sequence[MinimumCoverageItem]) -> MinimumCoverageSummary:
    required = [item for item in items if item.required]
    return MinimumCoverageSummary(
        total_required=len(required),
        covered=sum(1 for item in required if item.status == "covered"),
        covered_known_issue=sum(1 for item in required if item.status == "covered_known_issue"),
        covered_but_failing=sum(1 for item in required if item.status == "covered_but_failing"),
        not_executed=sum(1 for item in required if item.status == "not_executed"),
        missing=sum(1 for item in required if item.status == "missing"),
        skipped_by_scope=sum(1 for item in required if item.status == "skipped_by_scope"),
    )


def auth_known_keys(knowledge: Mapping[str, Any] | object) -> frozenset[str]:
    """Closed ``auth.*`` / ``auth_matrix.*`` set (Task 4 auth matrix handoff)."""
    if isinstance(knowledge, Mapping):
        auth = knowledge.get("auth") or {}
        matrix = knowledge.get("auth_matrix") or {}
    else:
        auth = getattr(knowledge, "auth", {}) or {}
        matrix = getattr(knowledge, "auth_matrix", {}) or {}
    if not isinstance(auth, Mapping) or not isinstance(matrix, Mapping):
        return frozenset()
    keys = {f"auth.{name}" for name in auth}
    keys |= {f"auth_matrix.{cell_id}" for cell_id in matrix}
    return frozenset(keys)


def journey_known_keys(keys: Iterable[str]) -> frozenset[str]:
    """Closed journey set for A4 / ``e2e_if_enabled`` (separate from auth matrix)."""
    return frozenset(key for key in keys if key)


def maps_from_advisory_mrc(
    minimum_required_coverage: Mapping[str, Any] | None,
) -> tuple[dict[str, MrcCategory], dict[str, MrcLayer], dict[str, str]]:
    """Build key → category / layer / mrc_id maps from advisory MRC.

    Supports legacy ``{category: [key, ...]}`` string arrays and structured
    ``{id, key, category, layer, required}`` items. Per-category string order
    assigns ``MRC-<PREFIX>-NNN`` ids (skill-shaped).
    """
    category_by_key: dict[str, MrcCategory] = {}
    layer_by_key: dict[str, MrcLayer] = {}
    mrc_id_by_key: dict[str, str] = {}
    if not minimum_required_coverage:
        return category_by_key, layer_by_key, mrc_id_by_key

    for category_name, entries in minimum_required_coverage.items():
        if category_name not in _KNOWN_CATEGORIES or not isinstance(entries, list):
            continue
        category: MrcCategory = category_name  # type: ignore[assignment]
        seq = 0
        for entry in entries:
            if isinstance(entry, str):
                key = entry
                if not key:
                    continue
                seq += 1
                category_by_key[key] = category
                layer_by_key.setdefault(key, _CATEGORY_DEFAULT_LAYER[category])
                mrc_id_by_key.setdefault(key, f"MRC-{_CATEGORY_MRC_PREFIX[category]}-{seq:03d}")
                continue
            if not isinstance(entry, Mapping):
                continue
            key_raw = entry.get("key")
            if not isinstance(key_raw, str) or not key_raw:
                continue
            key = key_raw
            entry_category = entry.get("category")
            resolved_category: MrcCategory = (
                entry_category  # type: ignore[assignment]
                if entry_category in _KNOWN_CATEGORIES
                else category
            )
            category_by_key[key] = resolved_category
            entry_layer = entry.get("layer")
            if entry_layer in {"api", "e2e", "both"}:
                layer_by_key[key] = entry_layer  # type: ignore[assignment]
            else:
                layer_by_key.setdefault(key, _CATEGORY_DEFAULT_LAYER[resolved_category])
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id:
                mrc_id_by_key[key] = entry_id
            else:
                seq += 1
                mrc_id_by_key.setdefault(key, f"MRC-{_CATEGORY_MRC_PREFIX[resolved_category]}-{seq:03d}")
    return category_by_key, layer_by_key, mrc_id_by_key


def mrc_closed_key_findings(
    items: Sequence[MinimumCoverageMatrixRow | MinimumCoverageItem | MrcObligation],
    *,
    constraint_keys: frozenset[str],
    auth_keys: frozenset[str],
    journey_keys: frozenset[str],
) -> tuple[MrcFinding, ...]:
    """Keys that violate the closed-set rule for their category, sorted unique."""
    allowed_constraint_or_auth = constraint_keys | auth_keys
    bad: set[str] = set()
    for item in items:
        category = getattr(item, "category", None)
        key = item.key
        if category is None:
            continue
        if category in _CONSTRAINT_OR_AUTH_CATEGORIES and key not in allowed_constraint_or_auth:
            bad.add(key)
        elif category in _JOURNEY_CATEGORIES and key not in journey_keys:
            bad.add(key)
    return tuple(MrcFinding(code="unknown_closed_key", key=key) for key in sorted(bad))


__all__ = [
    "MinimumCoverageItem",
    "MinimumCoverageMatrix",
    "MinimumCoverageMatrixRow",
    "MinimumCoverageResult",
    "MinimumCoverageSummary",
    "MrcCategory",
    "MrcFinding",
    "MrcFindingCode",
    "MrcItemStatus",
    "MrcLayer",
    "MrcMappingSource",
    "MrcObligation",
    "auth_known_keys",
    "journey_known_keys",
    "maps_from_advisory_mrc",
    "mrc_closed_key_findings",
    "summarize_minimum_coverage",
]
