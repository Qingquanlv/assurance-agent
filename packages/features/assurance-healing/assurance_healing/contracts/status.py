"""Healing episode status contract."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from assurance_healing.contracts.wire import FrozenContract, HexDigest
from assurance_intake.contracts import NonEmptyStr

RepairRoundKind = Literal["failure", "coverage"]

HealingStatusValue = Literal[
    "pending",
    "allocated",
    "proposed",
    "approved",
    "applied",
    "needs_review",
    "resolved",
    "not_needed",
    "skipped",
    "exhausted",
    "failed",
]


class HealingStatusV1(FrozenContract):
    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    episode_id: NonEmptyStr | None = None
    status: HealingStatusValue
    attempts_used: int = Field(ge=0)
    last_operation_id: NonEmptyStr | None = None
    last_record_key: NonEmptyStr | None = None
    candidate_digest: HexDigest | None = None
    baseline_digest: HexDigest | None = None
    policy_digest: HexDigest | None = None
    execution_evidence_digest: HexDigest | None = None
