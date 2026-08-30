"""Change-local adversarial discovery contracts."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

DiscoverySchemaVersion = Literal["1"]
DiscoverySurface = Literal["api"]
OracleKind = Literal["hard_oracle", "search_heuristic", "environment_oracle"]
CadenceProfile = Literal["pr", "nightly", "release"]
MinimizationStatus = Literal["raw", "minimizing", "minimized", "irreducible"]
FindingStatus = Literal["confirmed", "needs_review", "inconclusive_evidence"]
CampaignStatus = Literal[
    "completed",
    "stopped_budget",
    "stopped_fail_fast",
    "stopped_environment",
    "stopped_manual",
    "failed",
]


def _reject_unsafe_relpath(path: str, *, field_name: str) -> str:
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


class BudgetBounds(BaseModel):
    model_config = _FROZEN

    max_rounds: int | None = Field(default=None, ge=1)
    max_wall_clock_seconds: int | None = Field(default=None, ge=1)
    max_requests: int | None = Field(default=None, ge=1)


class CampaignSpec(BaseModel):
    """Frozen input for discovery campaign specification."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    campaign_id: NonEmptyStr
    change_id: NonEmptyStr
    surfaces: tuple[DiscoverySurface, ...] = Field(min_length=1)
    strategy_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    cadence_profile: CadenceProfile
    budget: BudgetBounds | None = None
    oracle_set_ref: NonEmptyStr | None = None


class CounterexampleReplay(BaseModel):
    model_config = _FROZEN

    attempts: int = Field(ge=0)
    reproduced: int = Field(ge=0)
    artifact_refs: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _reproduced_within_attempts(self) -> Self:
        if self.reproduced > self.attempts:
            raise ValueError("replay.reproduced cannot exceed replay.attempts")
        return self


class MinimizationInfo(BaseModel):
    model_config = _FROZEN

    status: MinimizationStatus
    parent_counterexample_id: NonEmptyStr | None = None


class Counterexample(BaseModel):
    """Confirmed or candidate discovery finding."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    counterexample_id: NonEmptyStr
    campaign_id: NonEmptyStr
    round_id: NonEmptyStr
    surface: DiscoverySurface
    technique: NonEmptyStr
    obligation_ids: tuple[NonEmptyStr, ...] = ()
    oracle_id: NonEmptyStr
    oracle_kind: OracleKind
    environment_digest: NonEmptyStr
    generated_file_digests: dict[str, str] = Field(default_factory=dict)
    setup: dict[str, Any] = Field(default_factory=dict)
    actions: tuple[dict[str, Any], ...] = ()
    observed: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    seed: int
    minimization: MinimizationInfo
    replay: CounterexampleReplay
    finding_status: FindingStatus

    @field_validator("generated_file_digests")
    @classmethod
    def _safe_digest_keys(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            _reject_unsafe_relpath(key, field_name="generated_file_digests")
        return value

    @model_validator(mode="after")
    def _confirmed_requires_hard_oracle_and_full_replay(self) -> Self:
        if self.finding_status != "confirmed":
            return self
        if self.oracle_kind != "hard_oracle":
            raise ValueError("confirmed finding requires oracle_kind=hard_oracle")
        if not self.oracle_id:
            raise ValueError("confirmed finding requires hard oracle_id present")
        if self.replay.attempts < 1 or self.replay.reproduced != self.replay.attempts:
            raise ValueError("confirmed finding requires replay.reproduced == replay.attempts >= 1")
        return self


class CampaignResult(BaseModel):
    """Thin summary document for a discovery campaign."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    campaign_id: NonEmptyStr
    change_id: NonEmptyStr
    status: CampaignStatus
    surfaces: tuple[DiscoverySurface, ...] = Field(min_length=1)
    rounds_completed: int = Field(ge=0)
    sample_count: int | None = Field(default=None, ge=0)
    seed: int | None = None
    counterexample_count: int = Field(ge=0)
    confirmed_count: int = Field(ge=0)
    stop_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _confirmed_not_above_total(self) -> Self:
        if self.confirmed_count > self.counterexample_count:
            raise ValueError("confirmed_count cannot exceed counterexample_count")
        return self
