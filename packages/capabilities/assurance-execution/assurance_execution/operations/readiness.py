"""Read-only host checks for a selected, short-lived Collector receipt."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1, HostReadinessFileV1
from graph_engine.plugin_api import FrozenModel
from typing import Literal
from assurance_execution.contracts.readiness import (
    ManagedSutReadinessSelectionV1,
    VerificationReadinessBindingV1,
)
from graph_engine.plugin_api import SecretPort
from assurance_execution.operations.host_secrets import read_host_secret_model, HostSecretDocumentError


class HostReadinessError(ValueError):
    """Fixed, public-safe readiness failure category."""


def process_birth_identity(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Collector process identity is unavailable") from error
    observed = completed.stdout.strip()
    if completed.returncode != 0 or not observed:
        raise ValueError("Collector process is not alive")
    return "sha256:" + hashlib.sha256(observed.encode()).hexdigest()


def authenticate_collector_readiness(
    receipt: CollectorReadinessReceiptV1,
    *,
    sut_instance_id: str,
    execution_id: str,
    configuration_digest: str,
    authorization_scope_digest: str,
    activity_receipt_digest: str,
) -> None:
    expected = (
        sut_instance_id,
        execution_id,
        configuration_digest,
        authorization_scope_digest,
        activity_receipt_digest,
    )
    actual = (
        receipt.sut_instance_id,
        receipt.execution_id,
        receipt.configuration_digest,
        receipt.authorization_scope_digest,
        receipt.activity_receipt_digest,
    )
    if actual != expected:
        raise ValueError("Collector readiness selection or authorization drifted")
    now = datetime.now(timezone.utc)
    if (
        not receipt.issued_at <= receipt.checked_at <= now < receipt.expires_at
        or (now - receipt.checked_at).total_seconds() > 30
    ):
        raise ValueError("Collector readiness is stale or from the future")
    _authenticate_collector_files(receipt)
    if process_birth_identity(receipt.collector_pid) != receipt.collector_process_birth_identity:
        raise ValueError("Collector process birth identity drifted")
    endpoint = urlsplit(receipt.collector_endpoint)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.port is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Collector readiness endpoint must be a fixed loopback HTTP URL")
    try:
        deadline = time.monotonic() + 2
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            with client.stream("GET", receipt.collector_endpoint) as response:
                if response.status_code != 200:
                    raise ValueError("Collector readiness endpoint is unavailable")
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > 4096 or time.monotonic() > deadline:
                        raise ValueError("Collector readiness response is oversized")
        if hashlib.sha256(content).hexdigest() != receipt.endpoint_response_digest or json.loads(content) != {
            "probe_nonce": receipt.probe_nonce,
            "execution_id": receipt.execution_id,
        }:
            raise ValueError("Collector readiness endpoint identity drifted")
    except (httpx.HTTPError, OSError, json.JSONDecodeError) as error:
        raise ValueError("Collector readiness endpoint is unavailable") from error
    if (
        process_birth_identity(receipt.collector_pid) != receipt.collector_process_birth_identity
        or datetime.now(timezone.utc) >= receipt.expires_at
    ):
        raise ValueError("Collector readiness expired during the check")


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

    selection = authenticate_host_selection(binding, secret_port=secret_port)
    sut_failed = False
    try:
        authenticate_managed_sut_readiness(selection, source_root=source_root, secret_port=secret_port)
    except HostSecretDocumentError:
        raise
    except (ValueError, OSError):
        sut_failed = True
    if sut_failed:
        raise HostReadinessError("managed SUT readiness authentication failed")
    if binding.validation_profile == "api_db_trace.v1":
        if binding.collector_handle is None:
            raise HostReadinessError("Collector/OTel readiness receipt is required")
        receipt, _ = read_host_secret_model(
            secret_port,
            binding.collector_handle,
            CollectorReadinessReceiptV1,
            category="Collector/OTel readiness receipt",
        )
        collector_failed = False
        try:
            authenticate_collector_readiness(
                receipt,
                sut_instance_id=selection.verification.sut_instance_id,
                execution_id=selection.execution_id,
                configuration_digest=binding.configuration_digest,
                authorization_scope_digest=selection.authorization_scope_digest,
                activity_receipt_digest=selection.activity_receipt_digest,
            )
        except (ValueError, OSError):
            collector_failed = True
        if collector_failed:
            raise HostReadinessError("Collector/OTel readiness authentication failed")
    return selection


class CollectorQualification(FrozenModel):
    validation_profile: Literal["api_db_trace.v1"]
    configuration_digest: str
    collector_artifact: HostReadinessFileV1
    collector_config: HostReadinessFileV1
    otel_dependencies: HostReadinessFileV1
    otel_qualification: HostReadinessFileV1


def _authenticate_collector_files(receipt: CollectorQualification | CollectorReadinessReceiptV1) -> None:
    for reference in (
        receipt.collector_artifact,
        receipt.collector_config,
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
            raise ValueError("Collector readiness file is not owned by the host")
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference.digest:
            raise ValueError("Collector artifact/configuration/OTel digest drifted")


def authenticate_collector_artifacts(secret_port: SecretPort, handle: str, configuration_digest: str) -> None:
    receipt, _ = read_host_secret_model(
        secret_port, handle, CollectorQualification, category="Collector/OTel qualification"
    )
    if receipt.configuration_digest != configuration_digest:
        raise HostReadinessError("Collector/OTel configuration drifted")
    _authenticate_collector_files(receipt)
