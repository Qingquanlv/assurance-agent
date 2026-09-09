"""Per-attempt User SUT lifecycle and private retained authority documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from assurance_execution.contracts.verification import ManagedSutAuthorityV1

from dataclasses import dataclass
import httpx
from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.canonical import canonical_digest
from pydantic import JsonValue
from graph_engine.plugin_api import SecretPort, FrozenModel
from typing import Literal
from assurance_execution.operations.host_secrets import read_host_secret_model
from assurance_execution.operations.managed_sut import _authenticate_artifact_tree
from assurance_execution.contracts.agent import ExecutionPrepareInputV1, VerifiedExecutionPrepareV1
from assurance_execution.contracts.verification import FrozenUserInputsV1
from assurance_execution.operations.agent_skills import _execution_id, authenticate_generation_result
from assurance_execution.operations.managed_sut import ManagedUserSutHost, authenticate_reviewed_sut_source
from assurance_execution.operations.verification_manifest import build_managed_sut_authority
from assurance_generation.contracts.admission import admit_verified_generation
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


SUT_SECRET_HANDLES = ("managed-sut.admin-password", "managed-sut.reset-password", "managed-sut.secret-key")


class UserInvocationHost(FrozenModel):
    kind: Literal["user-invocation-host.v1"]
    authority_root: str
    fault: str
    frozen_artifact_ref: EvidenceArtifactRefV1


def authenticate_user_invocation(
    secrets: SecretPort, authority_handle: str, *, workspace_root: Path, source_root: Path
) -> UserInvocationHost:
    seed, _ = read_host_secret_model(
        secrets, authority_handle, UserInvocationHost, category="User invocation host"
    )
    private = _private_root(Path(seed.authority_root))
    workspace = workspace_root.resolve(strict=True)
    if private.is_relative_to(workspace) or workspace.is_relative_to(private):
        raise ValueError("NOT_READY: host authority must remain outside the project")
    if seed.frozen_artifact_ref.path != ".aa/user-oracle/runtime-lock.json":
        raise ValueError("NOT_READY: selected frozen artifact path is invalid")
    frozen = workspace / ".aa/user-oracle"
    raw = (frozen / "runtime-lock.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != seed.frozen_artifact_ref.digest:
        raise ValueError("NOT_READY: selected frozen artifact lock changed")
    locked = json.loads(raw)
    if locked.get("fault") != seed.fault or locked.get("schema_version") != "1":
        raise ValueError("NOT_READY: selected frozen artifact configuration changed")
    _authenticate_artifact_tree(
        workspace,
        frozen,
        {
            **locked["files"],
            "runtime-lock.json": "sha256:" + seed.frozen_artifact_ref.digest,
        },
    )
    fixture = source_root / "benchmark/assurance-product/fixtures/user-oracle"
    for name in ("bootstrap.py", "qualify_runtime.py"):
        path = fixture / name
        if path.is_symlink() or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != locked[
            "files"
        ].get(name):
            raise ValueError("NOT_READY: installed lifecycle helper differs from frozen artifact")
    return seed


def _private_root(root: Path) -> Path:
    if root != root.resolve(strict=True) or root.is_symlink() or not root.is_dir():
        raise ValueError("NOT_READY: private host authority root is unavailable")
    details = root.stat()
    if details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError("NOT_READY: host authority root permissions are invalid")
    return root.resolve(strict=True)


def _authority_path(root: Path, execution_id: str) -> Path:
    if (
        re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", execution_id)
        is None
    ):
        raise ValueError("invalid authority execution identity")
    return _private_root(root) / f"{execution_id}.json"


def retain_authority(
    root: Path, execution_id: str, authority: ManagedSutAuthorityV1, *, project_root: Path
) -> None:
    private = _private_root(root)
    if private.is_relative_to(project_root.resolve()) or project_root.resolve().is_relative_to(private):
        raise ValueError("host authority files must remain outside project and candidate roots")
    path = _authority_path(private, execution_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        data = memoryview(authority.model_dump_json().encode())
        while data:
            data = data[os.write(fd, data) :]
        os.fsync(fd)
    finally:
        os.close(fd)


class RetainedUserAttempt(FrozenModel):
    authority: ManagedSutAuthorityV1
    verification: VerifiedExecutionPrepareV1
    credential: str
    credential_handle: str
    action_credential_handle: str
    input_digest: str
    prepared_input: dict[str, JsonValue] | None = None


def _read_retained(root: Path, execution_id: str) -> bytes:
    path = _authority_path(root, execution_id)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_mode & 0o077
            or details.st_uid != os.getuid()
        ):
            raise ValueError("NOT_READY: retained authority file is invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _read_attempt(root: Path, execution_id: str) -> RetainedUserAttempt:
    raw = _read_retained(root, execution_id)

    class RetainedSecret:
        def resolve(self, handle: str) -> bytes:
            return raw

    record, _ = read_host_secret_model(
        RetainedSecret(), "retained", RetainedUserAttempt, category="retained User attempt"
    )
    return record


def read_retained_authority(root: Path, execution_id: str) -> ManagedSutAuthorityV1:
    raw = _read_retained(root, execution_id)
    document = json.loads(raw)
    if isinstance(document, dict) and "authority" in document:
        return _read_attempt(root, execution_id).authority
    return ManagedSutAuthorityV1.model_validate(document)


def _retain_attempt(
    root: Path, execution_id: str, record: RetainedUserAttempt, *, update: bool = False
) -> None:
    path = _authority_path(root, execution_id)
    if update:
        current = _read_attempt(root, execution_id)
        if (
            current.prepared_input is not None and current.prepared_input != record.prepared_input
        ) or current.model_copy(update={"prepared_input": record.prepared_input}) != record:
            raise ValueError("NOT_READY: retained User attempt identity drifted")
        fd, temporary = tempfile.mkstemp(dir=root, prefix=".attempt-")
    else:
        temporary = str(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(record.model_dump_json().encode())
            stream.flush()
            os.fsync(fd)
        if update:
            os.replace(temporary, path)
        directory = os.open(root, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)
        if update and os.path.exists(temporary):
            os.unlink(temporary)


def authority_from_handle(raw: bytes, execution_id: str) -> ManagedSutAuthorityV1:
    document = json.loads(raw)
    if isinstance(document, dict) and document.get("kind") == "user-invocation-host.v1":
        return read_retained_authority(Path(document["authority_root"]), execution_id)
    return ManagedSutAuthorityV1.model_validate(document)


class AttemptSecrets:
    def __init__(self, base: SecretPort, values: dict[str, bytes]) -> None:
        self.base, self.values = base, values

    def resolve(self, handle: str) -> bytes:
        return self.values[handle] if handle in self.values else self.base.resolve(handle)


@dataclass
class UserAttempt:
    verification: VerifiedExecutionPrepareV1
    secrets: AttemptSecrets
    authority: ManagedSutAuthorityV1
    host: ManagedUserSutHost
    workspace_root: Path
    execution_id: str
    private_root: Path
    record: RetainedUserAttempt

    def retain_prepared(self, payload: dict[str, JsonValue]) -> None:
        self.record = self.record.model_copy(update={"prepared_input": payload})
        _retain_attempt(self.private_root, self.execution_id, self.record, update=True)

    def stop(self) -> None:
        self.host.stop_owned(workspace_root=self.workspace_root, authority=self.authority)


def start_user_attempt(
    root: ExecutionPrepareInputV1,
    *,
    source_root: Path,
    workspace_root: Path,
    attempt_key: AttemptKey,
    invocation_id: str,
    task_id: str,
    graph_instance_id: str,
    node_id: str,
    authorization_scope_digest: str,
    secrets: SecretPort,
    authority_handle: str,
    credential_handle: str,
) -> UserAttempt:
    """Create an owned SUT only after authenticating this attempt's generated closure."""
    generation = root.generation_result
    if (
        generation is None
        or generation.case_execution_plan_ref is None
        or root.validation_profile != "api_db.v1"
    ):
        raise ValueError("NOT_READY: current API DB generation is required")
    authenticate_generation_result(root, workspace_root)
    seed = authenticate_user_invocation(
        secrets, authority_handle, workspace_root=workspace_root, source_root=source_root
    )
    private = Path(seed.authority_root)
    admission = admit_verified_generation(
        workspace_root,
        workspace_root,
        change_id=root.change_id,
        coverage_epoch=root.coverage_epoch,
        plan_digest=root.plan_digest,
        plan_ref=root.plan_ref,
        reviewed_case=generation.reviewed_case,
        validation_profile=root.validation_profile,
        selected_test_families=root.selected_test_families,
        capability_leafs=root.capability_leafs,
        case_execution_plan_ref=generation.case_execution_plan_ref,
    )
    if len(admission.closed_mapping.selected) != 1 or len(admission.machine_plans.cases) != 1:
        raise ValueError("NOT_READY: User benchmark requires exactly one current generated case")
    nodeid = admission.closed_mapping.selected[0]
    execution_id = _execution_id(attempt_key, nodeid)
    # O_EXCL retention prevents another attempt from replacing authority under this identity.
    if _authority_path(private, execution_id).exists():
        raise ValueError("NOT_READY: attempt already owns an execution; reconcile required")
    base = workspace_root / ".aa" / "managed-user" / execution_id
    run = base / "runtime"
    host = ManagedUserSutHost(source_root=source_root, secret_port=secrets)
    frozen_ref = EvidenceArtifactRefV1.model_validate(seed.frozen_artifact_ref)
    if frozen_ref.path != ".aa/user-oracle/runtime-lock.json":
        raise ValueError("NOT_READY: selected frozen artifact path is invalid")
    prepared = host.prepare(
        workspace_root=workspace_root,
        project_dir=base / "source",
        run_root=run,
        fault=seed.fault,
        frozen_artifact=workspace_root / ".aa/user-oracle",
        frozen_artifact_digest="sha256:" + frozen_ref.digest,
    )
    authenticate_reviewed_sut_source(workspace_root, admission.machine_plans.cases[0], prepared)
    try:
        started = host.start(workspace_root=workspace_root, prepare_receipt=run / "harness-prepare.json")
    except BaseException:
        receipt = run / "owned-process.json"
        if receipt.is_file():
            # The fixed stop command authenticates the sealed receipt independently.
            host.stop(
                workspace_root=workspace_root,
                receipt_path=receipt,
                instance_id=str(json.loads(receipt.read_bytes())["instance_id"]),
            )
        raise
    try:

        def ref(path: Path) -> EvidenceArtifactRefV1:
            return EvidenceArtifactRefV1(
                path=path.relative_to(workspace_root).as_posix(),
                digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        prepare_ref, start_ref = ref(run / "harness-prepare.json"), ref(run / "owned-process.json")
        token_path = run / ".ownership-token"
        token_stat = token_path.stat()
        activity_digest = canonical_digest(
            {
                "attempt_key": attempt_key.digest,
                "invocation_id": invocation_id,
                "task_id": task_id,
                "graph_instance_id": graph_instance_id,
                "node_id": node_id,
                "workspace_identity_digest": authorization_scope_digest,
            }
        )
        authority = build_managed_sut_authority(
            run_root=run,
            ownership_token_path=token_path,
            ownership_token_device=token_stat.st_dev,
            ownership_token_inode=token_stat.st_ino,
            ownership_token_digest="sha256:" + hashlib.sha256(token_path.read_bytes()).hexdigest(),
            prepare_receipt_digest=prepare_ref.digest,
            start_receipt_digest=start_ref.digest,
            authorization_scope_digest=authorization_scope_digest,
            activity_receipt_digest=activity_digest,
        )
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(
                str(started["base_url"]) + "/api/v1/base/access_token",
                json={
                    "username": "admin",
                    "password": secrets.resolve("managed-sut.admin-password").decode(),
                },
            )
            response.raise_for_status()
            token = response.json()["data"]["access_token"]
        credential_payload = {
            "token": token,
            "user_password": secrets.resolve(credential_handle).decode(),
        }
        if seed.fault in {"no-action", "skip-oracle", "db-unavailable"}:
            credential_payload["benchmark_fault"] = seed.fault
        credential = json.dumps(credential_payload).encode()
        user_inputs = FrozenUserInputsV1.model_validate(admission.machine_plans.cases[0].inputs)
        profile = VerifiedExecutionPrepareV1(
            validation_profile=root.validation_profile,
            case_execution_plan_ref=generation.case_execution_plan_ref,
            nodeid=nodeid,
            business_activation=BusinessActivation.for_trigger(
                f"coverage.{root.coverage_epoch}.execute"
                if root.repair_round == 0
                else f"healing.{root.repair_round}.run"
            ),
            sut_instance_id=str(started["instance_id"]),
            sut_base_url=str(started["base_url"]),
            managed_sqlite_path=str(started["sqlite_path"]),
            observer_sqlite_path=str(started["sqlite_path"]),
            user_inputs=user_inputs,
            managed_sut_prepare_receipt_ref=prepare_ref,
            managed_sut_start_receipt_ref=start_ref,
            managed_sut_authority_handle=authority_handle,
        )
        record = RetainedUserAttempt(
            authority=authority,
            verification=profile,
            credential=credential.decode(),
            credential_handle=credential_handle,
            action_credential_handle=admission.machine_plans.cases[0].action.credential_ref,
            input_digest=canonical_digest(root.model_dump(mode="json")),
        )
        _retain_attempt(private, execution_id, record)
        overlay = AttemptSecrets(
            secrets,
            {
                authority_handle: authority.model_dump_json().encode(),
                credential_handle: credential,
                admission.machine_plans.cases[0].action.credential_ref: credential,
            },
        )
        return UserAttempt(profile, overlay, authority, host, workspace_root, execution_id, private, record)
    except BaseException:
        host.stop(
            workspace_root=workspace_root,
            receipt_path=run / "owned-process.json",
            instance_id=str(started["instance_id"]),
        )
        raise


def recover_user_attempt(
    root: ExecutionPrepareInputV1,
    *,
    workspace_root: Path,
    source_root: Path,
    attempt_key: AttemptKey,
    secrets: SecretPort,
    authority_handle: str,
) -> UserAttempt | None:
    from assurance_generation.contracts.mapping import ClosedMappingV1

    authenticate_generation_result(root, workspace_root)
    seed, _ = read_host_secret_model(
        secrets, authority_handle, UserInvocationHost, category="User invocation host"
    )
    generation = root.generation_result
    if generation is None:
        raise ValueError("NOT_READY: current generation is required")
    mapping = ClosedMappingV1.model_validate_json((workspace_root / generation.mapping_ref.path).read_bytes())
    if len(mapping.selected) != 1:
        raise ValueError("NOT_READY: exactly one current generated case is required")
    execution_id = _execution_id(attempt_key, mapping.selected[0])
    private = _private_root(Path(seed.authority_root))
    workspace = workspace_root.resolve(strict=True)
    if private.is_relative_to(workspace) or workspace.is_relative_to(private):
        raise ValueError("NOT_READY: host authority must remain outside the project")
    path = _authority_path(private, execution_id)
    if not path.exists() and not path.is_symlink():
        if (workspace_root / ".aa/managed-user" / execution_id).exists():
            raise ValueError("NOT_READY: retained attempt authority is unavailable")
        return None
    record = _read_attempt(private, execution_id)
    if record.input_digest != canonical_digest(root.model_dump(mode="json")):
        raise ValueError("NOT_READY: retained attempt input drifted")
    overlay = AttemptSecrets(
        secrets,
        {
            authority_handle: record.authority.model_dump_json().encode(),
            record.credential_handle: record.credential.encode(),
            record.action_credential_handle: record.credential.encode(),
        },
    )
    return UserAttempt(
        record.verification,
        overlay,
        record.authority,
        ManagedUserSutHost(source_root=source_root, secret_port=overlay),
        workspace_root,
        execution_id,
        private,
        record,
    )
