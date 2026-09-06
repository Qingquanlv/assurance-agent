"""Create and authenticate host-owned verification manifests."""

from __future__ import annotations

import stat
import uuid
from collections.abc import Callable
from pathlib import Path

from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.canonical import canonical_digest

from assurance_execution.contracts.verification import (
    FrozenUserInputsV1,
    ManagedSutAuthorityV1,
    ManagedSutOwnershipTokenV1,
    ManagedSutV1,
    SqliteFileIdentityV1,
    VerificationManifestV1,
)
from assurance_generation.contracts.execution_plan import ValidationProfile


def build_managed_sut_authority(
    *,
    run_root: Path,
    ownership_token_path: Path,
    ownership_token_device: int,
    ownership_token_inode: int,
    ownership_token_digest: str,
    prepare_receipt_digest: str,
    start_receipt_digest: str,
    authorization_scope_digest: str,
    activity_receipt_digest: str,
) -> ManagedSutAuthorityV1:
    """Close host-retained lifecycle values without reading the receipt bundle."""
    return ManagedSutAuthorityV1(
        run_root=str(run_root),
        ownership_token=ManagedSutOwnershipTokenV1(
            path=str(ownership_token_path),
            device=ownership_token_device,
            inode=ownership_token_inode,
            digest=ownership_token_digest,
        ),
        prepare_receipt_digest=prepare_receipt_digest,
        start_receipt_digest=start_receipt_digest,
        authorization_scope_digest=authorization_scope_digest,
        activity_receipt_digest=activity_receipt_digest,
    )


def sqlite_file_identity(path: Path) -> SqliteFileIdentityV1:
    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("SQLite path must not be a symlink")
    try:
        resolved = supplied.resolve(strict=True)
        details = resolved.stat()
    except OSError as error:
        raise ValueError("SQLite database is missing or unreadable") from error
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("SQLite database must be a regular file")
    return SqliteFileIdentityV1(
        path=str(resolved),
        device=int(details.st_dev),
        inode=int(details.st_ino),
    )


def build_verification_manifest(
    *,
    change_id: str,
    case_id: str,
    nodeid: str,
    invocation_id: str,
    task_id: str,
    graph_instance_id: str,
    attempt_key: AttemptKey,
    business_activation: BusinessActivation,
    coverage_epoch: int,
    repair_round: int,
    authorization_scope_digest: str,
    activity_receipt_digest: str,
    plan_ref: str,
    plan_digest: str,
    case_execution_plan_ref: str,
    case_execution_plan_digest: str,
    spec_digest: str,
    mapping_digest: str,
    sut_digest: str,
    technical_config_digest: str,
    validation_profile: ValidationProfile,
    sut_base_url: str,
    sut_instance_id: str,
    sut_sqlite_path: Path,
    sqlite_path: Path,
    username: str,
    email: str,
    evidence_root: str,
    execution_id: str | None = None,
) -> VerificationManifestV1:
    frozen_execution_id = execution_id or str(uuid.uuid4())
    managed_sqlite = Path(sut_sqlite_path).resolve(strict=True)
    sqlite = sqlite_file_identity(sqlite_path)
    return VerificationManifestV1(
        execution_id=frozen_execution_id,
        change_id=change_id,
        case_id=case_id,
        nodeid=nodeid,
        invocation_id=invocation_id,
        task_id=task_id,
        graph_instance_id=graph_instance_id,
        attempt_key=attempt_key,
        business_activation=business_activation,
        coverage_epoch=coverage_epoch,
        repair_round=repair_round,
        authorization_scope_digest=authorization_scope_digest,
        activity_receipt_digest=activity_receipt_digest,
        plan_ref=plan_ref,
        plan_digest=plan_digest,
        case_execution_plan_ref=case_execution_plan_ref,
        case_execution_plan_digest=case_execution_plan_digest,
        spec_digest=spec_digest,
        mapping_digest=mapping_digest,
        sut_digest=sut_digest,
        technical_config_digest=technical_config_digest,
        validation_profile=validation_profile,
        sut=ManagedSutV1(
            instance_id=sut_instance_id,
            base_url=sut_base_url,
            sqlite_path=str(managed_sqlite),
        ),
        sqlite=sqlite,
        inputs=FrozenUserInputsV1(username=username, email=email),
        evidence_root=f"{evidence_root.rstrip('/')}/{frozen_execution_id}",
    )


def authenticate_verification_manifest(
    manifest: VerificationManifestV1,
    *,
    manifest_digest: str,
    attempt_key: AttemptKey,
    invocation_id: str,
    nodeid: str,
    sqlite_path: Path,
    username: str,
    email: str,
    authorization_scope_digest: str,
    activity_receipt_digest: str,
) -> VerificationManifestV1:
    if canonical_digest(manifest.model_dump(mode="json")) != manifest_digest:
        raise ValueError("verification manifest digest does not match")
    if manifest.attempt_key != attempt_key or manifest.invocation_id != invocation_id:
        raise ValueError("verification manifest workflow identity does not match")
    if (
        manifest.authorization_scope_digest != authorization_scope_digest
        or manifest.activity_receipt_digest != activity_receipt_digest
    ):
        raise ValueError("verification manifest authorization scope or activity receipt does not match")
    if manifest.nodeid != nodeid:
        raise ValueError("verification manifest nodeid does not match")
    if manifest.inputs.username != username:
        raise ValueError("verification manifest username does not match")
    if manifest.inputs.email != email:
        raise ValueError("verification manifest email does not match")
    supplied = Path(sqlite_path).resolve()
    if str(supplied) != manifest.sqlite.path:
        raise ValueError("verification manifest SQLite path does not match")
    if sqlite_file_identity(supplied) != manifest.sqlite:
        raise ValueError("verification manifest SQLite file identity does not match")
    return manifest


def allocate_user_inputs(
    collides: Callable[[str, str], bool],
    *,
    token_factory: Callable[[], str] | None = None,
) -> FrozenUserInputsV1:
    """Allocate a unique User business key with at most three pre-freeze checks."""
    factory = token_factory or (lambda: uuid.uuid4().hex[:12])
    for _ in range(3):
        token = factory().lower()
        if not token or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789" for character in token):
            raise ValueError("generated User token is not canonical")
        username = f"qa_{token}"[:20]
        email = f"{username}@example.com"
        if not collides(username, email):
            return FrozenUserInputsV1(username=username, email=email)
    raise ValueError("could not allocate User inputs after three collision checks")


__all__ = [
    "allocate_user_inputs",
    "authenticate_verification_manifest",
    "build_managed_sut_authority",
    "build_verification_manifest",
    "sqlite_file_identity",
]
