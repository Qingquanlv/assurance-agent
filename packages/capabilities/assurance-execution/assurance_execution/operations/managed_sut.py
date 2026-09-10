"""Authenticate a provided User SUT binding. Execution does not own the process."""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
from graph_engine.plugin_api import SecretPort

from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1
from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
from assurance_execution.contracts.verification import SqliteFileIdentityV1, UserAttemptAuthorityV1
from assurance_execution.operations.common import InputError
from assurance_execution.operations.host_secrets import read_host_secret_model
from assurance_execution.operations.verification_manifest import sqlite_file_identity


def authenticate_sut_binding(
    workspace: Path,
    profile: VerifiedExecutionPrepareV1,
    *,
    secret_port: SecretPort | None,
    authorization_scope_digest: str,
    activity_receipt_digest: str,
) -> tuple[Path, Path, SqliteFileIdentityV1, str]:
    del workspace
    authority, authority_digest = read_host_secret_model(
        secret_port,
        profile.managed_sut_authority_handle,
        UserAttemptAuthorityV1,
        category="User attempt authority",
    )
    if (
        authority.authorization_scope_digest != authorization_scope_digest
        or authority.activity_receipt_digest != activity_receipt_digest
    ):
        raise InputError("User attempt authority does not match authenticated prepare")
    try:
        sqlite_path = Path(profile.managed_sqlite_path).resolve(strict=True)
        observer_path = Path(profile.observer_sqlite_path).resolve(strict=True)
        bound_path = Path(authority.sqlite_path).resolve(strict=True)
    except OSError as error:
        raise InputError("SUT SQLite path is missing") from error
    if sqlite_path != observer_path or sqlite_path != bound_path:
        raise InputError("SUT SQLite path does not match the provided binding")
    details = sqlite_path.stat()
    if sqlite_path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise InputError("SUT SQLite path is not a regular file")
    identity = sqlite_file_identity(sqlite_path)
    if (
        authority.sut_base_url != profile.sut_base_url
        or authority.instance_id != profile.sut_instance_id
    ):
        raise InputError("SUT binding identity does not match profile")
    return sqlite_path, observer_path, identity, authority_digest


def probe_sut_http(base_url: str) -> None:
    try:
        with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client:
            response = client.get(f"{base_url.rstrip('/')}/openapi.json")
    except (httpx.HTTPError, OSError) as error:
        raise InputError("SUT HTTP readiness probe failed") from error
    if response.status_code != 200:
        raise InputError("SUT HTTP readiness probe failed")


def authenticate_managed_sut_readiness(
    selection: ManagedSutReadinessSelectionV1,
    *,
    source_root: Path | None = None,
    secret_port: SecretPort | None = None,
) -> None:
    del source_root
    authenticate_sut_binding(
        Path(selection.workspace_root),
        selection.verification,
        secret_port=secret_port,
        authorization_scope_digest=selection.authorization_scope_digest,
        activity_receipt_digest=selection.activity_receipt_digest,
    )
    probe_sut_http(selection.verification.sut_base_url)
