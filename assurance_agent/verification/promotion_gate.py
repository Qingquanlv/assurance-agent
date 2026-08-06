"""Pure §11.5 Promotion Gate evaluation (no filesystem I/O).

Fail-closed: returns typed reason codes for every violated check. Apply/rollback
live in ``workflow.improvements.promotion_delivery`` and must not mutate
canonical tests unless this gate returns ``ok=True``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvements import DeliveryKind, ImprovementState
from assurance_agent.artifacts.models.promotion import RegressionCandidate, TestPromotionManifest

PromotionGateReasonCode = Literal[
    "improvement_not_approved",
    "improvement_version_drift",
    "improvement_delivery_mismatch",
    "candidate_id_mismatch",
    "candidate_digest_mismatch",
    "source_bytes_missing",
    "source_digest_mismatch",
    "review_subject_digest_mismatch",
    "target_base_digest_drift",
    "must_not_exist_violation",
    "unresolved_conflict",
    "replay_failed",
    "fix_protocol_failed",
    "not_minimized",
    "change_private_dependency",
    "suite_failed",
    "semantic_duplicate",
    "unauthorized_write",
]

PROMOTION_GATE_REASON_CODES: frozenset[str] = frozenset(
    {
        "improvement_not_approved",
        "improvement_version_drift",
        "improvement_delivery_mismatch",
        "candidate_id_mismatch",
        "candidate_digest_mismatch",
        "source_bytes_missing",
        "source_digest_mismatch",
        "review_subject_digest_mismatch",
        "target_base_digest_drift",
        "must_not_exist_violation",
        "unresolved_conflict",
        "replay_failed",
        "fix_protocol_failed",
        "not_minimized",
        "change_private_dependency",
        "suite_failed",
        "semantic_duplicate",
        "unauthorized_write",
    }
)

_MINIMIZED = frozenset({"minimized", "irreducible"})
_PHASE1_TARGET_PREFIXES = ("tests/api/", "tests/testdata/")

# Mechanical Phase-1 heuristics for Change-private / absolute path deps in source text.
_CHANGE_PRIVATE_PATTERNS = (
    re.compile(r"\bqa\.changes\b"),
    re.compile(r"qa/changes/"),
    re.compile(r"\bfrom\s+qa\.changes\b"),
    re.compile(r"""(?:from|import)\s+['"]?/?(?:Users|home|tmp)/"""),
    re.compile(r"""['"]/(?:Users|home|var|tmp)/[^'"]+['"]"""),
)


@dataclass(frozen=True)
class ApprovedImprovementView:
    """Thin approved-improvement projection injectable into the gate (Fake OK)."""

    improvement_id: str
    state: ImprovementState
    version: int
    review_subject_sha256: str | None
    delivery: DeliveryKind = DeliveryKind.TEST_PROMOTION


@dataclass(frozen=True)
class SuiteResults:
    """Injectable lint/static/test/flake outcomes (Fake can return all pass)."""

    lint_ok: bool = True
    static_ok: bool = True
    test_ok: bool = True
    flake_ok: bool = True

    @property
    def all_ok(self) -> bool:
        return self.lint_ok and self.static_ok and self.test_ok and self.flake_ok


@dataclass(frozen=True)
class PromotionGateDecision:
    ok: bool
    reasons: tuple[PromotionGateReasonCode, ...]


def evaluate_promotion_gate(
    manifest: TestPromotionManifest,
    *,
    candidate: RegressionCandidate,
    source_bytes: Mapping[str, bytes],
    target_bytes_on_disk: Mapping[str, bytes | None],
    approved_improvement: ApprovedImprovementView,
    expected_improvement_version: int,
    replay_ok: bool,
    suite_results: SuiteResults,
    before_fix_failed: bool = False,
    after_fix_passed: bool = False,
    authorized_targets: Sequence[str] | None = None,
    unresolved_conflict: bool = False,
) -> PromotionGateDecision:
    """Evaluate §11.5 Promotion Gate checks; pass only when all are green."""
    reasons: list[PromotionGateReasonCode] = []

    # 1. Improvement approved + version not drifted + delivery kind.
    if approved_improvement.improvement_id != manifest.improvement_id:
        reasons.append("improvement_not_approved")
    elif approved_improvement.state is not ImprovementState.APPROVED:
        reasons.append("improvement_not_approved")
    if approved_improvement.version != expected_improvement_version:
        reasons.append("improvement_version_drift")
    if approved_improvement.delivery is not DeliveryKind.TEST_PROMOTION:
        reasons.append("improvement_delivery_mismatch")

    # 2. Candidate, source bytes, review subject digest consistent.
    if candidate.candidate_id != manifest.candidate_id:
        reasons.append("candidate_id_mismatch")
    expected_candidate_digest = manifest.digests.get("candidate")
    actual_candidate_digest = sha256_bytes(canonical_json_bytes(candidate))
    if expected_candidate_digest is None or expected_candidate_digest != actual_candidate_digest:
        reasons.append("candidate_digest_mismatch")

    review_digest = manifest.digests.get("review_subject")
    if (
        review_digest is None
        or approved_improvement.review_subject_sha256 is None
        or review_digest != approved_improvement.review_subject_sha256
    ):
        reasons.append("review_subject_digest_mismatch")

    for mapping in manifest.mappings:
        payload = source_bytes.get(mapping.source)
        if payload is None:
            reasons.append("source_bytes_missing")
            continue
        actual = sha256_bytes(payload)
        if actual != mapping.source_sha256:
            reasons.append("source_digest_mismatch")
            continue
        declared = candidate.source_files.get(mapping.source)
        if declared is not None and declared != actual:
            reasons.append("source_digest_mismatch")

    # 3 + 8. Target base / must_not_exist / Phase1 semantic duplicate.
    for mapping in manifest.mappings:
        on_disk = target_bytes_on_disk.get(mapping.target)
        exists = on_disk is not None
        if mapping.must_not_exist:
            if exists:
                assert on_disk is not None
                if sha256_bytes(on_disk) != mapping.source_sha256:
                    reasons.append("semantic_duplicate")
                else:
                    reasons.append("must_not_exist_violation")
            continue
        # base-digest path
        if not exists:
            reasons.append("target_base_digest_drift")
            continue
        assert on_disk is not None
        if mapping.target_base_sha256 is None or sha256_bytes(on_disk) != mapping.target_base_sha256:
            reasons.append("target_base_digest_drift")

    if unresolved_conflict:
        reasons.append("unresolved_conflict")

    # 4. Independent replay success (injected).
    if not replay_ok:
        reasons.append("replay_failed")

    # 5. before-fix fail / after-fix pass OR explicit isolation_strategy.
    isolation = (manifest.isolation_strategy or "").strip()
    if not isolation and not (before_fix_failed and after_fix_passed):
        reasons.append("fix_protocol_failed")

    # 6. Minimized + mechanical hygiene (Phase1).
    if candidate.minimization_status not in _MINIMIZED:
        reasons.append("not_minimized")
    for mapping in manifest.mappings:
        if not any(
            mapping.target.startswith(prefix) and mapping.target != prefix.rstrip("/")
            for prefix in _PHASE1_TARGET_PREFIXES
        ):
            reasons.append("unauthorized_write")
        payload = source_bytes.get(mapping.source)
        if payload is not None and _has_change_private_dependency(payload):
            reasons.append("change_private_dependency")

    # 7. lint/static/test/flake suite results (injected).
    if not suite_results.all_ok:
        reasons.append("suite_failed")

    # 9. Write-set ⊆ authorized targets (manifest + optional Improvement auth).
    authorized = set(manifest.write_authorization)
    if authorized_targets is not None:
        authorized &= set(authorized_targets)
    for mapping in manifest.mappings:
        if mapping.target not in authorized:
            reasons.append("unauthorized_write")
        if mapping.target not in manifest.write_authorization:
            reasons.append("unauthorized_write")

    # Deduplicate while preserving first-seen order (one primary code per check).
    ordered = tuple(dict.fromkeys(reasons))
    return PromotionGateDecision(ok=not ordered, reasons=ordered)


def _has_change_private_dependency(payload: bytes) -> bool:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return any(pattern.search(text) for pattern in _CHANGE_PRIVATE_PATTERNS)


__all__ = [
    "PROMOTION_GATE_REASON_CODES",
    "ApprovedImprovementView",
    "PromotionGateDecision",
    "PromotionGateReasonCode",
    "SuiteResults",
    "evaluate_promotion_gate",
]
