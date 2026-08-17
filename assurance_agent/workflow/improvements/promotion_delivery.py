"""``delivery: test_promotion`` — gate-checked apply + content-addressed rollback.

Canonical writes go to ``project_root`` only (never change-local ``tests/**``).
Receipts stay change-relative under ``discovery/candidates/<id>/``.

Schema wiring for ``operation:test-promotion-apply`` / ``rollback`` is deferred;
call these functions directly (or wrap later in the operation handler).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.paths import DISCOVERY_CANDIDATE_NAME, existing_with_alias
from assurance_agent.artifacts.models.promotion import (
    PromotionReceipt,
    RegressionCandidate,
    TestPromotionManifest,
    WriteSetEntry,
)
from assurance_agent.exceptions import AaError
from assurance_agent.verification.promotion_gate import (
    ApprovedImprovementView,
    PromotionGateReasonCode,
    SuiteResults,
    evaluate_promotion_gate,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes

__all__ = [
    "PromotionDeliveryError",
    "apply_test_promotion",
    "as_approved_view",
    "rollback_test_promotion",
]


class PromotionDeliveryError(AaError):
    """Fail-closed promotion apply/rollback rejection."""

    def __init__(
        self,
        message: str,
        *,
        reasons: Sequence[PromotionGateReasonCode | str] = (),
    ) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(message)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _receipt_id(improvement_id: str, candidate_id: str, applied_at: str) -> str:
    raw = f"{improvement_id}:{candidate_id}:{applied_at}"
    return "PROM-RCPT-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _candidate_dir(change_dir: Path, candidate_id: str) -> Path:
    return change_dir / "discovery" / "candidates" / candidate_id


def _receipt_path(change_dir: Path, candidate_id: str) -> Path:
    return _candidate_dir(change_dir, candidate_id) / "promotion-receipt.json"


def _backup_dir(change_dir: Path, candidate_id: str) -> Path:
    return _candidate_dir(change_dir, candidate_id) / "promotion-backup"


def _backup_blob_path(change_dir: Path, candidate_id: str, digest: str) -> Path:
    hex_part = digest.removeprefix("sha256:")
    return _backup_dir(change_dir, candidate_id) / hex_part


def _load_candidate(change_dir: Path, candidate_id: str) -> RegressionCandidate:
    path = existing_with_alias(_candidate_dir(change_dir, candidate_id) / DISCOVERY_CANDIDATE_NAME)
    if path is None:
        raise PromotionDeliveryError(
            f"candidate missing at {_candidate_dir(change_dir, candidate_id) / DISCOVERY_CANDIDATE_NAME}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PromotionDeliveryError(f"candidate YAML invalid at {path}")
    return RegressionCandidate.model_validate(raw)


def _load_existing_receipt(change_dir: Path, candidate_id: str) -> PromotionReceipt | None:
    path = _receipt_path(change_dir, candidate_id)
    if not path.is_file():
        return None
    return PromotionReceipt.model_validate_json(path.read_text(encoding="utf-8"))


def _read_source_bytes(change_dir: Path, manifest: TestPromotionManifest) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for mapping in manifest.mappings:
        path = change_dir / mapping.source
        if not path.is_file():
            raise PromotionDeliveryError(
                f"source missing: {mapping.source}",
                reasons=("source_bytes_missing",),
            )
        # Refuse symlink escape out of change_dir.
        try:
            resolved = path.resolve(strict=True)
            root = change_dir.resolve(strict=True)
        except OSError as exc:
            raise PromotionDeliveryError(f"cannot resolve source {mapping.source}") from exc
        if not resolved.is_relative_to(root):
            raise PromotionDeliveryError(
                f"source escapes change_dir: {mapping.source}",
                reasons=("source_digest_mismatch",),
            )
        out[mapping.source] = path.read_bytes()
    return out


def _read_target_bytes(project_root: Path, manifest: TestPromotionManifest) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for mapping in manifest.mappings:
        path = project_root / mapping.target
        if path.is_symlink():
            raise PromotionDeliveryError(
                f"refusing symlink target: {mapping.target}",
                reasons=("unresolved_conflict",),
            )
        if path.is_file():
            out[mapping.target] = path.read_bytes()
        else:
            out[mapping.target] = None
    return out


def as_approved_view(projection: object) -> ApprovedImprovementView:
    """Adapt an ``ImprovementProjection`` (duck-typed) into the gate view."""
    return ApprovedImprovementView(
        improvement_id=projection.improvement_id,  # type: ignore[attr-defined]
        state=projection.state,  # type: ignore[attr-defined]
        version=projection.version,  # type: ignore[attr-defined]
        review_subject_sha256=projection.review_subject_sha256,  # type: ignore[attr-defined]
        delivery=projection.delivery,  # type: ignore[attr-defined]
    )


def apply_test_promotion(
    project_root: Path,
    change_dir: Path,
    manifest: TestPromotionManifest,
    *,
    approved_improvement: ApprovedImprovementView,
    expected_improvement_version: int,
    replay_ok: bool,
    suite_results: SuiteResults | None = None,
    before_fix_failed: bool = False,
    after_fix_passed: bool = False,
    authorized_targets: Sequence[str] | None = None,
    clock: Callable[[], str] | None = None,
) -> PromotionReceipt:
    """Apply promotion when the gate is green; write receipt under change_dir."""
    existing = _load_existing_receipt(change_dir, manifest.candidate_id)
    if existing is not None and existing.status == "applied":
        raise PromotionDeliveryError(
            "promotion receipt already applied; refuse re-apply",
            reasons=("unresolved_conflict",),
        )

    candidate = _load_candidate(change_dir, manifest.candidate_id)
    source_bytes = _read_source_bytes(change_dir, manifest)
    target_bytes = _read_target_bytes(project_root, manifest)

    # Content-addressed pre-check: source sha must match before any mutation.
    for mapping in manifest.mappings:
        actual = sha256_bytes(source_bytes[mapping.source])
        if actual != mapping.source_sha256:
            raise PromotionDeliveryError(
                f"source digest mismatch before apply: {mapping.source}",
                reasons=("source_digest_mismatch",),
            )

    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=source_bytes,
        target_bytes_on_disk=target_bytes,
        approved_improvement=approved_improvement,
        expected_improvement_version=expected_improvement_version,
        replay_ok=replay_ok,
        suite_results=suite_results or SuiteResults(),
        before_fix_failed=before_fix_failed,
        after_fix_passed=after_fix_passed,
        authorized_targets=authorized_targets,
    )
    if not decision.ok:
        raise PromotionDeliveryError(
            f"promotion gate failed: {', '.join(decision.reasons)}",
            reasons=decision.reasons,
        )

    write_set: list[WriteSetEntry] = []
    written: list[tuple[str, bytes | None]] = []
    try:
        for mapping in manifest.mappings:
            before = target_bytes.get(mapping.target)
            before_sha = None if before is None else sha256_bytes(before)
            if before is not None and before_sha is not None:
                blob = _backup_blob_path(change_dir, manifest.candidate_id, before_sha)
                atomic_write_bytes(blob, before)

            payload = source_bytes[mapping.source]
            after_sha = sha256_bytes(payload)
            dest = project_root / mapping.target
            atomic_write_bytes(dest, payload)
            written.append((mapping.target, before))

            # Verify after_sha256 matches on-disk bytes.
            on_disk = dest.read_bytes()
            if sha256_bytes(on_disk) != after_sha:
                raise PromotionDeliveryError(
                    f"after-write digest mismatch: {mapping.target}",
                    reasons=("source_digest_mismatch",),
                )
            write_set.append(
                WriteSetEntry(
                    path=mapping.target,
                    before_sha256=before_sha,
                    after_sha256=after_sha,
                )
            )
        applied_at = (clock or _utc_now)()
        receipt = PromotionReceipt(
            schema_version="1",
            receipt_id=_receipt_id(manifest.improvement_id, manifest.candidate_id, applied_at),
            improvement_id=manifest.improvement_id,
            candidate_id=manifest.candidate_id,
            applied_at=applied_at,
            write_set=tuple(write_set),
            status="applied",
            source_digests={m.source: m.source_sha256 for m in manifest.mappings},
            write_authorization=manifest.write_authorization,
        )
        atomic_write_bytes(
            _receipt_path(change_dir, manifest.candidate_id),
            canonical_json_bytes(receipt),
        )
    except Exception:
        # Receipt publication is part of the same logical transaction. Restore
        # every canonical target if either a target write or the receipt fails.
        for path_rel, prior in reversed(written):
            dest = project_root / path_rel
            if prior is None:
                dest.unlink(missing_ok=True)
            else:
                atomic_write_bytes(dest, prior)
        raise
    return receipt


def rollback_test_promotion(
    project_root: Path,
    receipt: PromotionReceipt,
    *,
    change_dir: Path | None = None,
    clock: Callable[[], str] | None = None,
) -> PromotionReceipt:
    """Restore exact write-set from an applied receipt; status → rolled_back."""
    del clock  # reserved for future receipt timestamps
    if receipt.status != "applied":
        raise PromotionDeliveryError(
            f"promotion receipt not applied (status={receipt.status})",
            reasons=("unresolved_conflict",),
        )

    # Phase 1: validate the complete rollback set and load every backup before
    # mutating any canonical target. This prevents a late drift/missing-backup
    # finding from leaving earlier targets already rolled back.
    prepared: list[tuple[WriteSetEntry, Path, bytes, bytes | None]] = []
    for entry in reversed(receipt.write_set):
        dest = project_root / entry.path
        if dest.is_symlink():
            raise PromotionDeliveryError(
                f"refusing symlink target on rollback: {entry.path}",
                reasons=("unresolved_conflict",),
            )
        if not dest.is_file():
            raise PromotionDeliveryError(
                f"canonical target drifted since apply: {entry.path} is missing",
                reasons=("target_base_digest_drift",),
            )
        current = dest.read_bytes()
        if sha256_bytes(current) != entry.after_sha256:
            raise PromotionDeliveryError(
                f"canonical target drifted since apply: {entry.path}",
                reasons=("target_base_digest_drift",),
            )

        if entry.before_sha256 is None:
            prepared.append((entry, dest, current, None))
            continue

        if change_dir is None:
            raise PromotionDeliveryError(
                "change_dir required to restore before-bytes from content-addressed backup",
            )
        blob = _backup_blob_path(change_dir, receipt.candidate_id, entry.before_sha256)
        if not blob.is_file():
            raise PromotionDeliveryError(
                f"rollback backup missing for {entry.path} ({entry.before_sha256})",
            )
        prior = blob.read_bytes()
        if sha256_bytes(prior) != entry.before_sha256:
            raise PromotionDeliveryError(
                f"rollback backup digest mismatch for {entry.path}",
                reasons=("source_digest_mismatch",),
            )
        prepared.append((entry, dest, current, prior))

    rolled = receipt.model_copy(update={"status": "rolled_back"})
    mutated: list[tuple[Path, bytes]] = []
    try:
        for _entry, dest, current, prior in prepared:
            if prior is None:
                dest.unlink()
            else:
                atomic_write_bytes(dest, prior)
            mutated.append((dest, current))
        if change_dir is not None:
            atomic_write_bytes(
                _receipt_path(change_dir, receipt.candidate_id),
                canonical_json_bytes(rolled),
            )
    except Exception:
        for dest, applied_bytes in reversed(mutated):
            atomic_write_bytes(dest, applied_bytes)
        raise
    return rolled
