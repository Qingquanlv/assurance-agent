"""Change-local adversarial discovery schemas (Phase 1, API vertical slice).

Documents under ``discovery/**`` — CampaignSpec, OracleSetSnapshot, RoundDecision,
GeneratedManifest, Counterexample, CampaignResult — plus nested Strategy/Oracle/
Replay models. Fail-closed enums; path safety on generated manifests; confirmed
findings require a hard oracle and full deterministic replay.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

DiscoverySchemaVersion = Literal["1"]
# Phase 1 vertical slice is API-only; later phases widen this closed set.
DiscoverySurface = Literal["api"]
OracleKind = Literal["hard_oracle", "search_heuristic", "environment_oracle"]
CadenceProfile = Literal["pr", "nightly", "release"]
GeneratedFileRole = Literal["search_test", "overlay", "testdata"]
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
AuthIsolationMode = Literal["deny_cross_tenant", "deny_cross_role", "require_auth"]

_PHASE1_TARGET_PREFIX = "tests/api/"


def _reject_unsafe_relpath(path: str, *, field_name: str) -> str:
    """Reject absolute paths, empty/`.`/`..` segments, and symlink-escape strings."""
    if not path or path.startswith("/") or path.startswith("\\") or path.startswith("~"):
        raise ValueError(f"{field_name} must be a safe project-relative path")
    if "\\" in path or "\x00" in path:
        raise ValueError(f"{field_name} must be a safe project-relative path")
    if len(path) >= 2 and path[1] == ":":
        raise ValueError(f"{field_name} must be a safe project-relative path")
    # Reject `..` anywhere (including `foo/./../../bar` symlink-escape patterns).
    if ".." in path:
        raise ValueError(f"{field_name} must be a safe project-relative path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must be a safe project-relative path")
    return path


def _require_phase1_api_target(path: str, *, field_name: str) -> str:
    safe = _reject_unsafe_relpath(path, field_name=field_name)
    if not safe.startswith(_PHASE1_TARGET_PREFIX) or safe == _PHASE1_TARGET_PREFIX.rstrip("/"):
        raise ValueError(f"{field_name} must be under tests/api/** for Phase 1")
    return safe


class BudgetBounds(BaseModel):
    """Optional thin budget caps; controller must not exceed these."""

    model_config = _FROZEN

    max_rounds: int | None = Field(default=None, ge=1)
    max_wall_clock_seconds: int | None = Field(default=None, ge=1)
    max_requests: int | None = Field(default=None, ge=1)


class AdversarialStrategy(BaseModel):
    """Minimal strategy directory entry for Phase 1 API discovery."""

    model_config = _FROZEN

    strategy_id: NonEmptyStr
    version: NonEmptyStr
    surface: DiscoverySurface
    technique: NonEmptyStr
    oracle_family_ids: tuple[NonEmptyStr, ...] = ()
    budget: BudgetBounds | None = None


class CampaignSpec(BaseModel):
    """Frozen input for ``discovery/campaign-spec.yaml``."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    campaign_id: NonEmptyStr
    change_id: NonEmptyStr
    surfaces: tuple[DiscoverySurface, ...] = Field(min_length=1)
    strategy_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    cadence_profile: CadenceProfile
    budget: BudgetBounds | None = None
    oracle_set_ref: NonEmptyStr | None = None


class StatusCodeRule(BaseModel):
    """Machine-checkable HTTP status expectation."""

    model_config = _FROZEN

    allowed_codes: tuple[int, ...] = ()
    denied_codes: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _require_at_least_one_code(self) -> Self:
        if not self.allowed_codes and not self.denied_codes:
            raise ValueError("status_codes requires allowed_codes or denied_codes")
        return self


class AuthIsolationRule(BaseModel):
    """Machine-checkable auth/tenant isolation expectation."""

    model_config = _FROZEN

    mode: AuthIsolationMode
    subject_claim: NonEmptyStr | None = None
    resource_claim: NonEmptyStr | None = None


class OracleRule(BaseModel):
    """Frozen, structured oracle payload — not free-form LLM prose alone."""

    model_config = _FROZEN

    status_codes: StatusCodeRule | None = None
    auth_isolation: AuthIsolationRule | None = None

    @model_validator(mode="after")
    def _require_structured_rule(self) -> Self:
        if self.status_codes is None and self.auth_isolation is None:
            raise ValueError("oracle rule requires status_codes and/or auth_isolation")
        return self


class OracleSpec(BaseModel):
    model_config = _FROZEN

    oracle_id: NonEmptyStr
    kind: OracleKind
    surface: DiscoverySurface
    rule: OracleRule


class OracleSetSnapshot(BaseModel):
    """Frozen oracle set for ``discovery/oracle-set.yaml``."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    change_id: NonEmptyStr
    campaign_id: NonEmptyStr
    oracles: tuple[OracleSpec, ...] = Field(min_length=1)


class RoundDecision(BaseModel):
    """Immutable pre-execution decision for ``discovery/rounds/<id>/decision.json``."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    change_id: NonEmptyStr
    campaign_id: NonEmptyStr
    round_id: NonEmptyStr
    parent_round_ids: tuple[NonEmptyStr, ...] = ()
    strategy_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    seed: int
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class GeneratedFileEntry(BaseModel):
    model_config = _FROZEN

    source: NonEmptyStr
    target: NonEmptyStr
    sha256: NonEmptyStr
    role: GeneratedFileRole

    @field_validator("source")
    @classmethod
    def _safe_source(cls, value: str) -> str:
        return _reject_unsafe_relpath(value, field_name="source")

    @field_validator("target")
    @classmethod
    def _safe_phase1_target(cls, value: str) -> str:
        return _require_phase1_api_target(value, field_name="target")


class GeneratedManifest(BaseModel):
    """Authorized materialization map for ``discovery/rounds/<id>/generated-manifest.json``."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    change_id: NonEmptyStr
    campaign_id: NonEmptyStr
    round_id: NonEmptyStr
    parent_round_ids: tuple[NonEmptyStr, ...] = ()
    base_revision: NonEmptyStr
    strategy_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    files: tuple[GeneratedFileEntry, ...] = Field(min_length=1)
    execution_selection: tuple[NonEmptyStr, ...] = Field(min_length=1)
    oracle_refs: tuple[NonEmptyStr, ...] = ()
    seed: int

    @field_validator("execution_selection")
    @classmethod
    def _safe_execution_selection(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_phase1_api_target(path, field_name="execution_selection") for path in value)


class CounterexampleReplay(BaseModel):
    """Independent replay evidence for a counterexample."""

    model_config = _FROZEN

    attempts: int = Field(ge=0)
    reproduced: int = Field(ge=0)
    artifact_refs: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _reproduced_within_attempts(self) -> Self:
        if self.reproduced > self.attempts:
            raise ValueError("replay.reproduced cannot exceed replay.attempts")
        return self


# C3 telemetry (§5-C3): immutable per-attempt receipt. Not a MetricKey — report /
# expansion-gate input only. Path:
# ``discovery/counterexamples/<id>/replay/attempt-<n>.json``.
ReplayAttemptOutcome = Literal[
    "violate",
    "hold",
    "inconclusive",
    "environment_failure",
    "divergence",
]


class ReplayAttemptReceipt(BaseModel):
    """One immutable seed-replay attempt receipt (M3 Task 3 / C3)."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    counterexample_id: NonEmptyStr
    seed: int
    attempt_index: int = Field(ge=0)
    base_revision: NonEmptyStr
    oracle_set_digest: NonEmptyStr
    outcome: ReplayAttemptOutcome
    # Content digest of the structured observation (or empty observation marker).
    observed_digest: NonEmptyStr
    observed_status: int | None = None
    # Wall-clock stamp; excluded from payload digests used for determinism checks.
    recorded_at: NonEmptyStr | None = None


class MinimizationInfo(BaseModel):
    model_config = _FROZEN

    status: MinimizationStatus
    parent_counterexample_id: NonEmptyStr | None = None


class Counterexample(BaseModel):
    """Confirmed or candidate finding at ``discovery/counterexamples/<id>.yaml`` (§9.2)."""

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
    """Thin summary document for ``discovery/campaign-result.yaml``."""

    model_config = _FROZEN

    schema_version: DiscoverySchemaVersion
    campaign_id: NonEmptyStr
    change_id: NonEmptyStr
    status: CampaignStatus
    surfaces: tuple[DiscoverySurface, ...] = Field(min_length=1)
    rounds_completed: int = Field(ge=0)
    # Added after the initial Phase 1 artifact shipped.  Optional at the schema
    # boundary keeps historical campaign receipts readable; metric consumers
    # must fail closed when either identity field is absent.
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
