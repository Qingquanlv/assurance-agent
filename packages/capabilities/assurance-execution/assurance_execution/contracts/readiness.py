"""Host-only readiness documents; these never form candidate execution output."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    telemetry_handle: str | None = None
    configuration_digest: str = Field(pattern=_SHA)
    validation_profile: Literal["api_db.v1", "api_db_trace.v1"]
