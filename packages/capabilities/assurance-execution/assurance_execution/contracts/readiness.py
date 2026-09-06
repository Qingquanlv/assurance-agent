"""Host-only readiness documents; these never form candidate execution output."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator
from graph_engine.plugin_api import FrozenModel
from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1

_SHA = r"^[0-9a-f]{64}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def _absolute(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or str(path.resolve(strict=False)) != value:
        raise ValueError("host readiness path must be canonical and absolute")
    return value


class ManagedSutReadinessSelectionV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    workspace_root: str
    verification: VerifiedExecutionPrepareV1
    configuration_digest: str = Field(pattern=_SHA)
    execution_id: str = Field(pattern=_UUID)
    authorization_scope_digest: str = Field(pattern=_SHA)
    activity_receipt_digest: str = Field(pattern=_SHA)

    _workspace_path = field_validator("workspace_root")(_absolute)


class HostReadinessFileV1(FrozenModel):
    path: str
    digest: str = Field(pattern=_SHA)

    _path = field_validator("path")(_absolute)


class VerificationReadinessBindingV1(FrozenModel):
    selection_handle: str = Field(min_length=1)
    authority_handle: str = Field(min_length=1)
    collector_handle: str | None = None
    configuration_digest: str = Field(pattern=_SHA)
    validation_profile: Literal["api_db.v1", "api_db_trace.v1"]


class CollectorReadinessReceiptV1(FrozenModel):
    """T11 must produce this receipt through an authorized host secret handle.

    The endpoint must answer the nonce/execution probe and the host must retain
    the exact artifact, configuration, dependency and qualification documents.
    Mere endpoint liveness does not assert OTel compatibility.
    """

    schema_version: Literal["1"]
    validation_profile: Literal["api_db_trace.v1"]
    sut_instance_id: str = Field(min_length=1)
    execution_id: str = Field(pattern=_UUID)
    configuration_digest: str = Field(pattern=_SHA)
    authorization_scope_digest: str = Field(pattern=_SHA)
    activity_receipt_digest: str = Field(pattern=_SHA)
    collector_endpoint: str
    collector_pid: int = Field(gt=1, strict=True)
    collector_process_birth_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    collector_artifact: HostReadinessFileV1
    collector_config: HostReadinessFileV1
    otel_dependencies: HostReadinessFileV1
    otel_qualification: HostReadinessFileV1
    issued_at: AwareDatetime
    checked_at: AwareDatetime
    expires_at: AwareDatetime
    probe_nonce: str = Field(pattern=_SHA)
    endpoint_response_digest: str = Field(pattern=_SHA)

    @model_validator(mode="after")
    def _bounded_validity(self) -> Self:
        if not self.issued_at <= self.checked_at < self.expires_at:
            raise ValueError("Collector readiness times are not ordered")
        if (self.expires_at - self.issued_at).total_seconds() > 60:
            raise ValueError("Collector readiness lifetime exceeds 60 seconds")
        return self
