"""Closed runtime-fact contracts for verified business executions."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.frozen_json import FrozenJSONValue
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.execution_plan import ValidationProfile
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_SHA256 = r"^[0-9a-f]{64}$"
_TAGGED_SHA256 = r"^sha256:[0-9a-f]{64}$"
_EXECUTION_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class FrozenUserInputsV1(FrozenModel):
    username: str = Field(min_length=1, max_length=20)
    email: str = Field(min_length=3, max_length=255)
    is_active: Literal[True] = True
    is_superuser: Literal[False] = False
    dept_id: None = None

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if value != value.strip() or value.count("@") != 1:
            raise ValueError("email must be a canonical address")
        local, domain = value.rsplit("@", 1)
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("email must be a canonical address")
        return value


class SqliteFileIdentityV1(FrozenModel):
    path: str = Field(min_length=1)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        from pathlib import Path

        if not Path(value).is_absolute() or str(Path(value)) != value:
            raise ValueError("SQLite path must be canonical and absolute")
        return value


class SqliteObservationMetadataV1(FrozenModel):
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)


class ManagedSutOwnershipTokenV1(FrozenModel):
    path: str = Field(min_length=1)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    digest: str = Field(pattern=_TAGGED_SHA256)

    @field_validator("path")
    @classmethod
    def _canonical_path(cls, value: str) -> str:
        from pathlib import Path

        path = Path(value)
        if not path.is_absolute() or str(path.resolve(strict=False)) != value:
            raise ValueError("managed SUT ownership token path must be canonical and absolute")
        return value


class ManagedSutAuthorityV1(FrozenModel):
    """Host-retained trust anchor independent of the managed SUT receipt bundle."""

    schema_version: Literal["1"] = "1"
    run_root: str = Field(min_length=1)
    ownership_token: ManagedSutOwnershipTokenV1
    prepare_receipt_digest: str = Field(pattern=_SHA256)
    start_receipt_digest: str = Field(pattern=_SHA256)
    authorization_scope_digest: str = Field(pattern=_SHA256)
    activity_receipt_digest: str = Field(pattern=_SHA256)

    @field_validator("run_root")
    @classmethod
    def _canonical_run_root(cls, value: str) -> str:
        from pathlib import Path

        path = Path(value)
        if not path.is_absolute() or str(path.resolve(strict=False)) != value:
            raise ValueError("managed SUT authority run root must be canonical and absolute")
        return value

    @model_validator(mode="after")
    def _fixed_token_path(self) -> Self:
        from pathlib import Path

        if Path(self.ownership_token.path) != Path(self.run_root) / ".ownership-token":
            raise ValueError("managed SUT authority token path does not match its run root")
        return self


class ManagedSutV1(FrozenModel):
    instance_id: str = Field(min_length=1)
    base_url: str = Field(pattern=r"^http://127\.0\.0\.1:[1-9][0-9]{0,4}$")
    sqlite_path: str = Field(min_length=1)

    @field_validator("sqlite_path")
    @classmethod
    def _absolute_sqlite_path(cls, value: str) -> str:
        from pathlib import Path

        if not Path(value).is_absolute() or str(Path(value)) != value:
            raise ValueError("managed SUT SQLite path must be canonical and absolute")
        return value


class VerificationManifestV1(FrozenModel):
    """Host-created frozen identity and environment binding for one execution."""

    schema_version: Literal["1"] = "1"
    execution_id: str = Field(pattern=_EXECUTION_ID)
    change_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    nodeid: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    graph_instance_id: str = Field(min_length=1)
    attempt_key: AttemptKey
    business_activation: BusinessActivation
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    authorization_scope_digest: str = Field(pattern=_SHA256)
    activity_receipt_digest: str = Field(pattern=_SHA256)
    plan_ref: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256)
    case_execution_plan_ref: str = Field(min_length=1)
    case_execution_plan_digest: str = Field(pattern=_SHA256)
    spec_digest: str = Field(pattern=_SHA256)
    mapping_digest: str = Field(pattern=_SHA256)
    sut_digest: str = Field(pattern=_SHA256)
    technical_config_digest: str = Field(pattern=_SHA256)
    validation_profile: ValidationProfile
    sut: ManagedSutV1
    sqlite: SqliteFileIdentityV1
    inputs: FrozenUserInputsV1
    evidence_root: str = Field(min_length=1)

    @field_validator("nodeid")
    @classmethod
    def _full_nodeid(cls, value: str) -> str:
        if "::" not in value or not value.startswith("tests/") or "\n" in value:
            raise ValueError("nodeid must be a full canonical pytest nodeid")
        return value

    @field_validator("plan_ref", "case_execution_plan_ref", "evidence_root")
    @classmethod
    def _relative_artifact_path(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("artifact path must be canonical and relative")
        return value

    @model_validator(mode="after")
    def _bind_environment(self) -> Self:
        if self.sut.sqlite_path != self.sqlite.path:
            raise ValueError("managed SUT and observer SQLite paths differ")
        expected_root = f"qa/changes/{self.change_id}/execution/{self.execution_id}"
        if self.evidence_root != expected_root:
            raise ValueError("evidence root does not bind the execution identity")
        return self


ObservationState = Literal["observed", "missing", "error", "timeout", "skipped"]


class ObservationV1(FrozenModel):
    """One observed runtime fact. Expected values and verdicts are deliberately absent."""

    schema_version: Literal["1"] = "1"
    execution_id: str = Field(pattern=_EXECUTION_ID)
    obligation_id: str = Field(min_length=1)
    state: ObservationState
    actual: FrozenJSONValue = None
    evidence_ref: EvidenceArtifactRefV1 | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _state_shape(self) -> Self:
        if self.state == "observed":
            if "actual" not in self.model_fields_set or self.evidence_ref is None or self.reason is not None:
                raise ValueError("observed facts require actual and evidence, without a reason")
        elif self.reason is None:
            raise ValueError("non-observed facts require a reason")
        return self


class EvidenceCompletionV1(FrozenModel):
    state: Literal["complete", "error", "timeout", "not_required"]
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _reason_matches_state(self) -> Self:
        if (self.reason is None) != (self.state in {"complete", "not_required"}):
            raise ValueError("completion errors and timeouts require a reason")
        return self


class VerificationEvidenceV1(FrozenModel):
    """Host-collected evidence index. It carries facts, never an asserted verdict."""

    schema_version: Literal["1"] = "1"
    execution_id: str = Field(pattern=_EXECUTION_ID)
    manifest_digest: str = Field(pattern=_SHA256)
    receipt_ref: EvidenceArtifactRefV1
    observations: tuple[ObservationV1, ...]
    host_completion: EvidenceCompletionV1
    collector_completion: EvidenceCompletionV1
    state: Literal["collected", "incomplete"]

    @model_validator(mode="after")
    def _closed_observations(self) -> Self:
        ids = tuple(item.obligation_id for item in self.observations)
        if len(ids) != len(set(ids)):
            raise ValueError("evidence contains duplicate obligation observations")
        if any(item.execution_id != self.execution_id for item in self.observations):
            raise ValueError("observation execution identity does not match evidence")
        completions = {self.host_completion.state, self.collector_completion.state}
        if self.state == "collected" and not completions <= {"complete", "not_required"}:
            raise ValueError("collected evidence requires completed host and collector lifecycles")
        return self


class VerifiedProcessLimitsV1(FrozenModel):
    timeout_seconds: float = Field(default=60, gt=0, le=60)
    max_frame_bytes: int = Field(default=256 * 1024, gt=0, le=256 * 1024)
    max_stderr_bytes: int = Field(default=8 * 1024 * 1024, gt=0, le=8 * 1024 * 1024)


class VerifiedProcessReceiptV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    command: tuple[str, ...]
    limits: VerifiedProcessLimitsV1
    exit_code: int | None
    report: dict[str, Any] | None
    reason: str | None
    request_count: int = Field(ge=0)
    stderr: str
    cleanup_confirmed: bool


__all__ = [
    "EvidenceCompletionV1",
    "FrozenUserInputsV1",
    "ManagedSutAuthorityV1",
    "ManagedSutOwnershipTokenV1",
    "ManagedSutV1",
    "ObservationState",
    "ObservationV1",
    "SqliteFileIdentityV1",
    "SqliteObservationMetadataV1",
    "VerificationEvidenceV1",
    "VerificationManifestV1",
    "VerifiedProcessLimitsV1",
    "VerifiedProcessReceiptV1",
]
