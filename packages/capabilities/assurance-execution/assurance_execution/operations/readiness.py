"""Read-only host checks for a selected SUT and telemetry qualification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from graph_engine.plugin_api import FrozenModel
from typing import Literal
from assurance_execution.contracts.readiness import (
    HostReadinessFileV1,
    ManagedSutReadinessSelectionV1,
    VerificationReadinessBindingV1,
)
from graph_engine.plugin_api import SecretPort
from assurance_execution.operations.host_secrets import read_host_secret_model, HostSecretDocumentError


class HostReadinessError(ValueError):
    """Fixed, public-safe readiness failure category."""


def authenticate_host_selection(
    binding: VerificationReadinessBindingV1,
    *,
    secret_port: SecretPort,
) -> ManagedSutReadinessSelectionV1:
    """Authenticate frozen selection without requiring a still-live dependency."""
    selection, _ = read_host_secret_model(
        secret_port,
        binding.selection_handle,
        ManagedSutReadinessSelectionV1,
        category="managed SUT readiness selection",
    )
    if (
        selection.configuration_digest != binding.configuration_digest
        or selection.verification.validation_profile != binding.validation_profile
        or selection.verification.managed_sut_authority_handle != binding.authority_handle
    ):
        raise HostReadinessError("managed SUT readiness selection disagrees with frozen configuration")
    return selection


def authenticate_host_readiness(
    binding: VerificationReadinessBindingV1,
    *,
    source_root: Path,
    secret_port: SecretPort,
) -> ManagedSutReadinessSelectionV1:
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    del source_root
    selection = authenticate_host_selection(binding, secret_port=secret_port)
    sut_failed = False
    try:
        authenticate_managed_sut_readiness(selection, secret_port=secret_port)
    except HostSecretDocumentError:
        raise
    except (ValueError, OSError):
        sut_failed = True
    if sut_failed:
        raise HostReadinessError("managed SUT readiness authentication failed")
    return selection


class TelemetryQualification(FrozenModel):
    validation_profile: Literal["api_db_trace.v1"]
    configuration_digest: str
    otel_artifact: HostReadinessFileV1
    otel_config: HostReadinessFileV1
    otel_dependencies: HostReadinessFileV1
    otel_qualification: HostReadinessFileV1


def _authenticate_telemetry_files(receipt: TelemetryQualification) -> None:
    for reference in (
        receipt.otel_artifact,
        receipt.otel_config,
        receipt.otel_dependencies,
        receipt.otel_qualification,
    ):
        path = Path(reference.path)
        details = path.stat()
        if (
            path.is_symlink()
            or not path.is_file()
            or details.st_nlink != 1
            or details.st_uid != os.getuid()
            or details.st_mode & 0o022
        ):
            raise ValueError("Telemetry readiness file is not owned by the host")
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference.digest:
            raise ValueError("Telemetry artifact/configuration/OTel digest drifted")


def authenticate_telemetry_artifacts(secret_port: SecretPort, handle: str, configuration_digest: str) -> None:
    receipt, _ = read_host_secret_model(
        secret_port, handle, TelemetryQualification, category="Telemetry qualification"
    )
    if receipt.configuration_digest != configuration_digest:
        raise HostReadinessError("Telemetry configuration drifted")
    _authenticate_telemetry_files(receipt)
