"""Per-attempt User SUT lifecycle and private retained authority documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from assurance_execution.contracts.verification import ManagedSutAuthorityV1

from dataclasses import dataclass
import httpx
from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import SecretPort
from assurance_execution.contracts.agent import ExecutionPrepareInputV1, VerifiedExecutionPrepareV1
from assurance_execution.contracts.verification import FrozenUserInputsV1
from assurance_execution.operations.agent_skills import _execution_id, authenticate_generation_result
from assurance_execution.operations.managed_sut import ManagedUserSutHost, authenticate_reviewed_sut_source
from assurance_execution.operations.verification_manifest import build_managed_sut_authority
from assurance_generation.contracts.admission import admit_verified_generation
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def _private_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
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
    private = root.resolve(strict=True)
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


def read_retained_authority(root: Path, execution_id: str) -> ManagedSutAuthorityV1:
    path = _authority_path(root, execution_id)
    details = path.stat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_nlink != 1
        or details.st_mode & 0o077
        or details.st_uid != os.getuid()
    ):
        raise ValueError("NOT_READY: retained authority file is invalid")
    return ManagedSutAuthorityV1.model_validate_json(path.read_bytes())


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

    def stop(self) -> None:
        self.host.stop(
            workspace_root=self.workspace_root,
            receipt_path=self.workspace_root / self.verification.managed_sut_start_receipt_ref.path,
            instance_id=self.verification.sut_instance_id,
        )


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
    seed = json.loads(secrets.resolve(authority_handle))
    if (
        not isinstance(seed, dict)
        or set(seed) != {"kind", "authority_root", "fault", "frozen_artifact_ref"}
        or seed["kind"] != "user-invocation-host.v1"
    ):
        raise ValueError("NOT_READY: invocation host selection is invalid")
    private = _private_root(Path(seed["authority_root"]))
    if private.is_relative_to(workspace_root.resolve()) or workspace_root.resolve().is_relative_to(private):
        raise ValueError("NOT_READY: host authority must remain outside the project")
    generation = root.generation_result
    if (
        generation is None
        or generation.case_execution_plan_ref is None
        or root.validation_profile != "api_db.v1"
    ):
        raise ValueError("NOT_READY: current API DB generation is required")
    authenticate_generation_result(root, workspace_root)
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
    frozen_ref = EvidenceArtifactRefV1.model_validate(seed["frozen_artifact_ref"])
    if frozen_ref.path != ".aa/user-oracle/runtime-lock.json":
        raise ValueError("NOT_READY: selected frozen artifact path is invalid")
    prepared = host.prepare(
        workspace_root=workspace_root,
        project_dir=base / "source",
        run_root=run,
        fault=str(seed["fault"]),
        frozen_artifact=workspace_root / ".aa/user-oracle",
        frozen_artifact_digest="sha256:" + frozen_ref.digest,
    )
    authenticate_reviewed_sut_source(workspace_root, admission.machine_plans.cases[0], prepared)
    started = host.start(workspace_root=workspace_root, prepare_receipt=run / "harness-prepare.json")
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
        credential = json.dumps(
            {"token": token, "user_password": secrets.resolve(credential_handle).decode()}
        ).encode()
        username = "u" + execution_id.replace("-", "")[:18]
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
            user_inputs=FrozenUserInputsV1(username=username, email=username + "@example.com"),
            managed_sut_prepare_receipt_ref=prepare_ref,
            managed_sut_start_receipt_ref=start_ref,
            managed_sut_authority_handle=authority_handle,
        )
        retain_authority(private, execution_id, authority, project_root=workspace_root)
        overlay = AttemptSecrets(
            secrets,
            {
                authority_handle: authority.model_dump_json().encode(),
                credential_handle: credential,
                admission.machine_plans.cases[0].action.credential_ref: credential,
            },
        )
        return UserAttempt(profile, overlay, authority, host, workspace_root, execution_id)
    except BaseException:
        host.stop(
            workspace_root=workspace_root,
            receipt_path=run / "owned-process.json",
            instance_id=str(started["instance_id"]),
        )
        raise
