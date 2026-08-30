"""inspect/quarantine-projection.json (must_compat): flaky isolation ledger.

M3 Task 4 / design §5-C3: flaky property/journey subjects enter quarantine with
replay-receipt evidence; release requires consecutive successful replays
(default N=2). Active subjects are excluded from A2/A4 *covered* but remain in
the declared denominator.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# Default consecutive successful seed-replays (outcome == violate) required to
# leave quarantine. Documented constant — policy override is out of scope here.
DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES: int = 2

QuarantineSchemaVersion = Literal["1"]
QuarantineSubjectKind = Literal["property", "journey", "obligation_key"]
QuarantineStatus = Literal["active", "released"]

QUARANTINE_PROJECTION_REL = "inspect/quarantine-projection.json"


class QuarantineEntry(BaseModel):
    """One quarantined (or released) subject row."""

    model_config = _FROZEN

    subject_kind: QuarantineSubjectKind
    subject_key: NonEmptyStr
    status: QuarantineStatus
    reason: NonEmptyStr
    entered_at: NonEmptyStr
    # Change-relative replay receipt paths; required on enter (fail-closed).
    evidence_refs: tuple[NonEmptyStr, ...] = ()
    release_requires: int = Field(default=DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES, ge=1)
    consecutive_successes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _fail_closed_enter_and_release(self) -> Self:
        if self.status == "active" and len(self.evidence_refs) < 1:
            raise ValueError("active quarantine requires evidence_refs (>=1 replay receipt)")
        if self.status == "released" and self.consecutive_successes < self.release_requires:
            raise ValueError(
                "released quarantine requires consecutive_successes "
                f">= release_requires ({self.release_requires})"
            )
        return self


class QuarantineProjection(BaseModel):
    """Authoritative quarantine ledger at ``inspect/quarantine-projection.json``."""

    model_config = _FROZEN

    schema_version: QuarantineSchemaVersion
    change_id: NonEmptyStr
    entries: tuple[QuarantineEntry, ...] = ()

    @field_validator("entries", mode="after")
    @classmethod
    def _deterministic_entry_order(cls, value: tuple[QuarantineEntry, ...]) -> tuple[QuarantineEntry, ...]:
        return tuple(sorted(value, key=lambda item: (item.subject_kind, item.subject_key)))


__all__ = [
    "DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES",
    "QUARANTINE_PROJECTION_REL",
    "QuarantineEntry",
    "QuarantineProjection",
    "QuarantineSchemaVersion",
    "QuarantineStatus",
    "QuarantineSubjectKind",
]
