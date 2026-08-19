"""Test-promotion schemas for RegressionCandidate / Manifest / Receipt (Phase 1).

Change-local layout (design §5.1 + Task 1 choice)::

    discovery/candidates/<candidate-id>/
      candidate.yaml            -> RegressionCandidate
      files/                    -> source bytes (not separately registered)
      promotion-manifest.yaml   -> TestPromotionManifest
      promotion-receipt.json    -> PromotionReceipt

Receipts stay change-relative under ``discovery/candidates/*/`` (not
``qa/improvements/**`` or ``inspect/``) so candidate, manifest, and receipt
archive together with the discovery tree. Apply/rollback ops are Task 2.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_kernel.artifacts.models.common import NonEmptyStr
from assurance_kernel.artifacts.models.discovery import DiscoverySurface, MinimizationStatus

_FROZEN = ConfigDict(frozen=True, extra="forbid")

PromotionSchemaVersion = Literal["1"]
PromotionReceiptStatus = Literal["applied", "rolled_back"]

_PHASE1_TARGET_PREFIXES = ("tests/api/", "tests/testdata/")


def _reject_unsafe_relpath(path: str, *, field_name: str) -> str:
    """Reject absolute paths, empty/`.`/`..` segments, and symlink-escape strings."""
    if not path or path.startswith("/") or path.startswith("\\") or path.startswith("~"):
        raise ValueError(f"{field_name} must be a safe project-relative path")
    if "\\" in path or "\x00" in path:
        raise ValueError(f"{field_name} must be a safe project-relative path")
    if len(path) >= 2 and path[1] == ":":
        raise ValueError(f"{field_name} must be a safe project-relative path")
    if ".." in path:
        raise ValueError(f"{field_name} must be a safe project-relative path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must be a safe project-relative path")
    return path


def _require_phase1_promotion_target(path: str, *, field_name: str) -> str:
    safe = _reject_unsafe_relpath(path, field_name=field_name)
    if not any(safe.startswith(prefix) and safe != prefix.rstrip("/") for prefix in _PHASE1_TARGET_PREFIXES):
        raise ValueError(f"{field_name} must be under tests/api/** or tests/testdata/** for Phase 1")
    return safe


class RegressionCandidate(BaseModel):
    """Potential long-lived regression asset derived from a confirmed CE (§11.2)."""

    model_config = _FROZEN

    schema_version: PromotionSchemaVersion
    candidate_id: NonEmptyStr
    change_id: NonEmptyStr
    campaign_id: NonEmptyStr
    counterexample_id: NonEmptyStr
    problem_id: NonEmptyStr | None = None
    oracle_id: NonEmptyStr
    surface: DiscoverySurface
    proposed_targets: tuple[NonEmptyStr, ...] = Field(min_length=1)
    source_files: dict[str, str] = Field(min_length=1)
    minimization_status: MinimizationStatus
    purpose: NonEmptyStr
    evidence_refs: tuple[NonEmptyStr, ...] = ()

    @field_validator("proposed_targets")
    @classmethod
    def _safe_phase1_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_phase1_promotion_target(path, field_name="proposed_targets") for path in value)

    @field_validator("source_files")
    @classmethod
    def _safe_source_files(cls, value: dict[str, str]) -> dict[str, str]:
        for key, digest in value.items():
            _reject_unsafe_relpath(key, field_name="source_files")
            if not digest:
                raise ValueError("source_files digests must be non-empty")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _safe_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_reject_unsafe_relpath(path, field_name="evidence_refs") for path in value)


class PromotionMapping(BaseModel):
    """One Change-local source → canonical target mapping with base digest policy."""

    model_config = _FROZEN

    source: NonEmptyStr
    target: NonEmptyStr
    source_sha256: NonEmptyStr
    target_base_sha256: str | None = None
    must_not_exist: bool = False

    @field_validator("source")
    @classmethod
    def _safe_source(cls, value: str) -> str:
        return _reject_unsafe_relpath(value, field_name="source")

    @field_validator("target")
    @classmethod
    def _safe_target(cls, value: str) -> str:
        return _require_phase1_promotion_target(value, field_name="target")

    @model_validator(mode="after")
    def _must_not_exist_xor_base_digest(self) -> Self:
        has_base = self.target_base_sha256 is not None
        if self.must_not_exist == has_base:
            raise ValueError("must_not_exist XOR target_base_sha256 required (exactly one)")
        if has_base and not self.target_base_sha256:
            raise ValueError("target_base_sha256 must be non-empty when present")
        return self


class TestPromotionManifest(BaseModel):
    """Authorized promotion plan for ``delivery: test_promotion`` (§11.4)."""

    # Prevent pytest from collecting this schema as a test class (name prefix).
    __test__ = False
    model_config = _FROZEN

    schema_version: PromotionSchemaVersion
    improvement_id: NonEmptyStr
    candidate_id: NonEmptyStr
    mappings: tuple[PromotionMapping, ...] = Field(min_length=1)
    problem_id: NonEmptyStr | None = None
    counterexample_id: NonEmptyStr
    oracle_id: NonEmptyStr
    replay_refs: tuple[NonEmptyStr, ...] = ()
    before_fix_revision: NonEmptyStr
    after_fix_revision: NonEmptyStr
    isolation_strategy: NonEmptyStr | None = None
    write_authorization: tuple[NonEmptyStr, ...] = Field(min_length=1)
    rollback_manifest: tuple[NonEmptyStr, ...] = ()
    digests: dict[str, str] = Field(default_factory=dict)

    @field_validator("replay_refs")
    @classmethod
    def _safe_replay_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_reject_unsafe_relpath(path, field_name="replay_refs") for path in value)

    @field_validator("write_authorization")
    @classmethod
    def _safe_write_authorization(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _require_phase1_promotion_target(path, field_name="write_authorization") for path in value
        )

    @field_validator("rollback_manifest")
    @classmethod
    def _safe_rollback_manifest(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_phase1_promotion_target(path, field_name="rollback_manifest") for path in value)

    @field_validator("digests")
    @classmethod
    def _nonempty_digests(cls, value: dict[str, str]) -> dict[str, str]:
        for key, digest in value.items():
            if not key or not digest:
                raise ValueError("digests keys and values must be non-empty")
        return value

    @model_validator(mode="after")
    def _targets_within_authorization(self) -> Self:
        authorized = set(self.write_authorization)
        for mapping in self.mappings:
            if mapping.target not in authorized:
                raise ValueError("mapping targets must be ⊆ write_authorization")
        for target in self.rollback_manifest:
            if target not in authorized:
                raise ValueError("rollback_manifest targets must be ⊆ write_authorization")
        return self


class WriteSetEntry(BaseModel):
    """One path actually written (or restored) by a promotion apply/rollback."""

    model_config = _FROZEN

    path: NonEmptyStr
    before_sha256: str | None = None
    after_sha256: NonEmptyStr

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _require_phase1_promotion_target(value, field_name="path")

    @field_validator("before_sha256")
    @classmethod
    def _nonempty_before(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("before_sha256 must be non-empty when present")
        return value


class PromotionReceipt(BaseModel):
    """Post-apply receipt; rollback uses this exact write-set (§11.5)."""

    model_config = _FROZEN

    schema_version: PromotionSchemaVersion
    receipt_id: NonEmptyStr
    improvement_id: NonEmptyStr
    candidate_id: NonEmptyStr
    applied_at: NonEmptyStr
    write_set: tuple[WriteSetEntry, ...] = Field(min_length=1)
    status: PromotionReceiptStatus
    source_digests: dict[str, str] = Field(default_factory=dict)
    # Snapshot of authorized targets at apply time (audit + write_set ⊆ check).
    write_authorization: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("write_authorization")
    @classmethod
    def _safe_write_authorization(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _require_phase1_promotion_target(path, field_name="write_authorization") for path in value
        )

    @field_validator("source_digests")
    @classmethod
    def _nonempty_source_digests(cls, value: dict[str, str]) -> dict[str, str]:
        for key, digest in value.items():
            if not key or not digest:
                raise ValueError("source_digests keys and values must be non-empty")
        return value

    @model_validator(mode="after")
    def _write_set_within_authorization(self) -> Self:
        authorized = set(self.write_authorization)
        for entry in self.write_set:
            if entry.path not in authorized:
                raise ValueError("write_set paths must be ⊆ write_authorization")
        return self
