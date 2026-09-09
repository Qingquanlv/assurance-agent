"""Shared authentication seam for host-retained managed SUT lifecycle authority."""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import stat
import signal
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast
from graph_engine.canonical import JSONValue, canonical_digest
from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1, CaseExecutionPlanSetV1
from graph_engine.plugin_api import SecretPort
from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1
from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
from assurance_execution.contracts.verification import ManagedSutAuthorityV1, SqliteFileIdentityV1
from assurance_execution.operations.common import InputError
from assurance_execution.operations.host_secrets import read_host_secret_model
from assurance_execution.operations.verification_manifest import sqlite_file_identity
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def _regular_input_file(workspace: Path, relative: str) -> Path:
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError(f"execution input escapes the attempt workspace: {relative}") from error
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise InputError(f"execution input is not a regular single-link file: {relative}")
    return path


def _receipt_document(
    workspace: Path,
    ref: EvidenceArtifactRefV1,
    label: str,
) -> tuple[Path, dict[str, Any]]:
    path = _regular_input_file(workspace, ref.path)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != ref.digest:
        raise InputError(f"managed SUT receipt digest changed: {label}")
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InputError(f"managed SUT receipt is invalid: {label}") from error
    if not isinstance(document, dict):
        raise InputError(f"managed SUT receipt is invalid: {label}")
    return path.resolve(strict=True), document


def _runtime_qualification_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def managed_sut_ownership_token(run_root: Path, authority: ManagedSutAuthorityV1) -> bytes:
    path = Path(authority.ownership_token.path)
    if path != run_root / ".ownership-token":
        raise InputError("independent managed SUT authority token path does not match")
    if path.is_symlink() or not path.is_file():
        raise InputError("managed SUT ownership token is missing")
    details = path.stat()
    if (
        details.st_nlink != 1
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
        or details.st_dev != authority.ownership_token.device
        or details.st_ino != authority.ownership_token.inode
    ):
        raise InputError("managed SUT ownership token permissions do not match")
    token = path.read_bytes()
    if len(token) != 32 or f"sha256:{hashlib.sha256(token).hexdigest()}" != authority.ownership_token.digest:
        raise InputError("managed SUT ownership token is invalid")
    return token


def _authenticate_managed_sut_seal(
    document: Mapping[str, Any],
    *,
    label: str,
    ownership_token: bytes,
) -> None:
    supplied = document.get("receipt_digest")
    unsigned = {key: value for key, value in document.items() if key != "receipt_digest"}
    encoded = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    expected = f"hmac-sha256:{hmac.new(ownership_token, encoded, hashlib.sha256).hexdigest()}"
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise InputError(f"managed SUT receipt seal changed: {label}")


def _authenticate_artifact_tree(
    workspace: Path, root: Path, expected: Mapping[str, str], *, mutable: frozenset[str] = frozenset()
) -> None:
    """Check the sealed file closure, without following links or loading SUT code."""
    root.relative_to(workspace)
    if root != root.resolve(strict=True):
        raise ValueError("artifact root contains a symbolic link")
    directories = {"."}
    for relative, digest in expected.items():
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError("artifact file digest is invalid")
        EvidenceArtifactRefV1(path=relative, digest=digest.removeprefix("sha256:"))
        directories.update(str(parent) for parent in PurePosixPath(relative).parents)
    actual: dict[str, str] = {}
    pending = [root]
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            if relative not in directories:
                raise ValueError(f"artifact contains an undeclared directory: {relative}")
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError(f"artifact member is not a regular single-link file: {relative}")
        elif relative not in mutable:
            actual[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("artifact file membership or byte digest changed")


def _authenticate_artifact_closure(workspace: Path, prepared: Mapping[str, Any]) -> None:
    files = prepared["source_files"]
    frozen = Path(prepared["frozen_artifact"])
    runtime = Path(prepared["sut_dir"])
    _authenticate_artifact_tree(
        workspace, frozen, {**files, "runtime-lock.json": prepared["frozen_artifact_digest"]}
    )
    runtime_files = {
        name.removeprefix("sut-source/"): digest
        for name, digest in files.items()
        if name.startswith("sut-source/") or name in {"requirements.in", "requirements.lock"}
    }
    _authenticate_artifact_tree(
        workspace,
        runtime,
        runtime_files,
        mutable=frozenset({"db.sqlite3", "db.sqlite3-journal", "db.sqlite3-wal", "db.sqlite3-shm"}),
    )


def authenticate_reviewed_sut_source(
    workspace: Path, plan: CaseExecutionPlanV1, prepared: Mapping[str, Any]
) -> None:
    """Bind reviewed reference bytes to the fixed harness's frozen and runtime files."""
    _, review = _receipt_document(workspace, plan.reviewed_case.review_ref, "case review")
    try:
        reviewed_paths = review["source_verification"]["reviewed_source_files"]
        files = prepared["source_files"]
        mapping = prepared["source_file_mapping"]
        frozen = Path(prepared["frozen_artifact"])
        runtime = Path(prepared["sut_dir"])
        if frozen.name != "user-oracle" or frozen.parent.name != ".aa":
            raise ValueError("reviewed source artifact path is invalid")
        _, frozen_lock = _receipt_document(
            workspace,
            EvidenceArtifactRefV1(
                path=(frozen / "runtime-lock.json").relative_to(workspace).as_posix(),
                digest=str(prepared["frozen_artifact_digest"]).removeprefix("sha256:"),
            ),
            "frozen artifact",
        )
        if (
            any(frozen_lock.get(key) != prepared.get(key) for key in ("source_digest", "runtime_digest"))
            or frozen_lock.get("files") != files
        ):
            raise ValueError("frozen artifact manifest disagrees with prepare receipt")
        if not isinstance(prepared.get("fault"), str) or frozen_lock.get("fault") != prepared["fault"]:
            raise ValueError("frozen artifact fault selection disagrees with prepare receipt")
        _authenticate_artifact_closure(workspace, prepared)
        if (
            not isinstance(reviewed_paths, list)
            or not reviewed_paths
            or len(set(reviewed_paths)) != len(reviewed_paths)
        ):
            raise ValueError("reviewed source list is empty or duplicated")
        preparation = {ref.path: ref for ref in plan.reviewed_case.preparation_refs}
        refs = [preparation[relative] for relative in sorted(reviewed_paths)]
        if (
            canonical_digest(cast(JSONValue, [ref.model_dump(mode="json") for ref in refs]))
            != plan.sut_digest
        ):
            raise ValueError("reviewed source projection changed")
        for ref in refs:
            file_mapping = mapping[ref.path]
            artifact_relative = file_mapping["frozen_path"]
            runtime_relative = (workspace / ref.path).relative_to(frozen.parent.parent).as_posix()
            if (
                set(file_mapping) != {"frozen_path", "runtime_path"}
                or file_mapping["runtime_path"] != runtime_relative
                or artifact_relative != "sut-source/" + runtime_relative
                or files[artifact_relative] != "sha256:" + ref.digest
            ):
                raise ValueError("reviewed source mapping disagrees with frozen artifact")
            for path in (workspace / ref.path, frozen / artifact_relative, runtime / runtime_relative):
                relative = path.relative_to(workspace).as_posix()
                member = _regular_input_file(workspace, relative)
                if any(parent.is_symlink() for parent in member.parents if parent != workspace):
                    raise ValueError("reviewed source path contains a symlink")
                if hashlib.sha256(member.read_bytes()).hexdigest() != ref.digest:
                    raise ValueError("reviewed source bytes disagree with runtime copy")
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise InputError(f"NOT_READY: reviewed source does not match managed SUT: {error}") from error


def authenticate_managed_sut_receipts(
    workspace: Path,
    profile: VerifiedExecutionPrepareV1,
    *,
    secret_port: SecretPort | None,
    authorization_scope_digest: str,
    activity_receipt_digest: str,
) -> tuple[Path, Path, SqliteFileIdentityV1, str]:
    authority, authority_digest = read_host_secret_model(
        secret_port,
        profile.managed_sut_authority_handle,
        ManagedSutAuthorityV1,
        category="independent managed SUT authority",
    )
    if (
        authority.authorization_scope_digest != authorization_scope_digest
        or authority.activity_receipt_digest != activity_receipt_digest
        or authority.prepare_receipt_digest != profile.managed_sut_prepare_receipt_ref.digest
        or authority.start_receipt_digest != profile.managed_sut_start_receipt_ref.digest
    ):
        raise InputError("independent managed SUT authority does not match authenticated prepare")
    try:
        workspace_root = workspace.resolve(strict=True)
        run_root = Path(authority.run_root).resolve(strict=True)
        run_root.relative_to(workspace_root)
        prepare_relative = (run_root / "harness-prepare.json").relative_to(workspace_root).as_posix()
        start_relative = (run_root / "owned-process.json").relative_to(workspace_root).as_posix()
    except (OSError, ValueError) as error:
        raise InputError("independent managed SUT authority run root is invalid") from error
    if (
        profile.managed_sut_prepare_receipt_ref.path != prepare_relative
        or profile.managed_sut_start_receipt_ref.path != start_relative
    ):
        raise InputError("independent managed SUT authority receipt paths do not match")
    ownership_token = managed_sut_ownership_token(run_root, authority)
    prepare_path, prepared = _receipt_document(workspace, profile.managed_sut_prepare_receipt_ref, "prepare")
    start_path, started = _receipt_document(workspace, profile.managed_sut_start_receipt_ref, "start")
    sut_dir = run_root / "sut"
    sqlite_path = sut_dir / "db.sqlite3"
    if prepare_path != run_root / "harness-prepare.json" or start_path != run_root / "owned-process.json":
        raise InputError("managed SUT receipt paths are not fixed to one run root")
    if (
        prepared.get("schema_version") != "1"
        or prepared.get("state") != "prepared"
        or started.get("schema_version") != "1"
        or started.get("state") != "started"
        or prepared.get("workspace_root") != str(workspace.resolve())
        or started.get("workspace_root") != str(workspace.resolve())
        or prepared.get("run_root") != str(run_root)
        or started.get("run_root") != str(run_root)
        or prepared.get("sut_dir") != str(sut_dir)
        or started.get("sut_dir") != str(sut_dir)
        or prepared.get("sqlite_path") != str(sqlite_path)
        or started.get("sqlite_path") != str(sqlite_path)
        or started.get("prepare_receipt") != str(prepare_path)
        or started.get("prepare_receipt_digest") != prepared.get("receipt_digest")
        or started.get("prepare_receipt_sha256") != profile.managed_sut_prepare_receipt_ref.digest
        or prepared.get("source_digest") != started.get("source_digest")
        or prepared.get("runtime_digest") != started.get("runtime_digest")
    ):
        raise InputError("managed SUT receipt identity does not match")
    _authenticate_managed_sut_seal(prepared, label="prepare", ownership_token=ownership_token)
    _authenticate_managed_sut_seal(started, label="start", ownership_token=ownership_token)
    _, plan_document = _receipt_document(workspace, profile.case_execution_plan_ref, "machine plan")
    plans = CaseExecutionPlanSetV1.model_validate(plan_document)
    if len(plans.cases) != 1:
        raise InputError("NOT_READY: reviewed source requires one machine plan")
    authenticate_reviewed_sut_source(workspace, plans.cases[0], prepared)
    qualification = prepared.get("runtime_qualification")
    if not isinstance(qualification, dict):
        raise InputError("managed SUT receipt runtime qualification is missing")
    if prepared.get("runtime_qualification_digest") != _runtime_qualification_digest(qualification):
        raise InputError("managed SUT receipt runtime qualification digest changed")
    if qualification.get("sqlite_engine") != "tortoise.backends.sqlite" or qualification.get(
        "sqlite_path"
    ) != str(sqlite_path):
        raise InputError("managed SUT effective SQLite config does not match")
    try:
        managed_path = Path(profile.managed_sqlite_path).resolve(strict=True)
        observer_path = Path(profile.observer_sqlite_path).resolve(strict=True)
    except OSError as error:
        raise InputError("managed SUT SQLite path is missing") from error
    if managed_path != sqlite_path or observer_path != sqlite_path:
        raise InputError("managed SUT receipt SQLite path does not match")
    identity = sqlite_file_identity(sqlite_path)
    expected_identity = identity.model_dump(mode="json")
    if (
        prepared.get("sqlite_identity") != expected_identity
        or started.get("sqlite_identity") != expected_identity
    ):
        raise InputError("managed SUT receipt SQLite stable identity does not match")
    if (
        started.get("instance_id") != profile.sut_instance_id
        or started.get("base_url") != profile.sut_base_url
    ):
        raise InputError("managed SUT receipt identity does not match profile")
    return managed_path, observer_path, identity, authority_digest


_MANAGED_HARNESS_SHA256 = "15ddae370a2474bb0e0a98bbc971c76a1d1ce06ea4ce9f5e2ec5cdcc2814c961"


class ManagedUserSutHost:
    """Drive only the repository's pinned benchmark harness through fixed argv.

    source_root is a host-selected qualified installation/source checkout, never
    the SUT/project root. Secrets cross only to the owned harness start process.
    """

    def __init__(self, *, source_root: Path, secret_port: SecretPort) -> None:
        self.source_root = source_root
        self.secret_port = secret_port

    def _call(self, command: str, arguments: list[str]) -> dict[str, Any]:
        import subprocess
        import sys

        script = self.source_root / "benchmark/assurance-product/user_oracle_harness.py"
        lock_path = self.source_root / "benchmark/assurance-product/fixtures/user-oracle/runner-lock.json"
        try:
            lock = json.loads(lock_path.read_bytes())
            if (
                script.is_symlink()
                or hashlib.sha256(script.read_bytes()).hexdigest() != _MANAGED_HARNESS_SHA256
                or lock["harness_sha256"] != _MANAGED_HARNESS_SHA256
            ):
                raise ValueError("pinned harness bytes changed")
        except (OSError, ValueError, KeyError) as error:
            raise ValueError("NOT_READY: fixed managed SUT harness unavailable or changed") from error
        environment = {key: value for key, value in os.environ.items() if not key.startswith("AA_SUT_")}
        if command == "start":
            for name, handle in {
                "AA_SUT_ADMIN_PASSWORD": "managed-sut.admin-password",
                "AA_SUT_RESET_PASSWORD": "managed-sut.reset-password",
                "AA_SUT_SECRET_KEY": "managed-sut.secret-key",
            }.items():
                environment[name] = self.secret_port.resolve(handle).decode("utf-8")
        result = subprocess.run(
            [sys.executable, str(script), command, *arguments],
            cwd=self.source_root,
            env=environment,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 256 * 1024 or len(result.stderr) > 8 * 1024 * 1024:
            raise ValueError(f"NOT_READY: fixed managed SUT {command} failed")
        document = json.loads(result.stdout)
        if not isinstance(document, dict) or document.get("schema_version") != "1":
            raise ValueError("NOT_READY: invalid managed SUT lifecycle receipt")
        return document

    def prepare(
        self,
        *,
        workspace_root: Path,
        project_dir: Path,
        run_root: Path,
        fault: str = "none",
        frozen_artifact: Path | None = None,
        frozen_artifact_digest: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "prepare",
            [
                "--workspace-root",
                str(workspace_root),
                "--project-dir",
                str(project_dir),
                "--run-root",
                str(run_root),
                "--fault",
                fault,
            ]
            + (
                []
                if frozen_artifact is None
                else [
                    "--frozen-artifact",
                    str(frozen_artifact),
                    "--frozen-artifact-digest",
                    str(frozen_artifact_digest),
                ]
            ),
        )

    def start(self, *, workspace_root: Path, prepare_receipt: Path) -> dict[str, Any]:
        return self._call(
            "start", ["--workspace-root", str(workspace_root), "--prepare-receipt", str(prepare_receipt)]
        )

    def preflight(self, *, workspace_root: Path, receipt_path: Path, instance_id: str) -> dict[str, Any]:
        return self._call(
            "preflight",
            [
                "--workspace-root",
                str(workspace_root),
                "--receipt",
                str(receipt_path),
                "--instance-id",
                instance_id,
            ],
        )

    def stop(self, *, workspace_root: Path, receipt_path: Path, instance_id: str) -> dict[str, Any]:
        return self._call(
            "stop",
            [
                "--workspace-root",
                str(workspace_root),
                "--receipt",
                str(receipt_path),
                "--instance-id",
                instance_id,
            ],
        )

    def stop_owned(self, *, workspace_root: Path, authority: ManagedSutAuthorityV1) -> None:
        """Clean the independently retained process even after runtime drift or exit."""
        from assurance_execution.operations.readiness import process_birth_identity

        run = Path(authority.run_root)
        run.relative_to(workspace_root.resolve(strict=True))
        token = managed_sut_ownership_token(run, authority)
        _, started = _receipt_document(
            workspace_root,
            EvidenceArtifactRefV1(
                path=(run / "owned-process.json").relative_to(workspace_root).as_posix(),
                digest=authority.start_receipt_digest,
            ),
            "owned start",
        )
        _authenticate_managed_sut_seal(started, label="owned start", ownership_token=token)
        if (
            started.get("run_root") != str(run)
            or started.get("workspace_root") != str(workspace_root.resolve())
            or started.get("state") != "started"
        ):
            raise ValueError("NOT_READY: retained process ownership drifted")
        stopped = run / "stopped-process.json"
        if stopped.exists() or stopped.is_symlink():
            path = _regular_input_file(workspace_root, stopped.relative_to(workspace_root).as_posix())
            document = json.loads(path.read_bytes())
            _authenticate_managed_sut_seal(document, label="owned stop", ownership_token=token)
            expected = {**started, "state": "stopped", "reason": "owned_sut_stopped"}
            if {k: v for k, v in document.items() if k != "receipt_digest"} != {
                k: v for k, v in expected.items() if k != "receipt_digest"
            }:
                raise ValueError("NOT_READY: retained cleanup identity drifted")
            return
        pid = started.get("pid")
        if type(pid) is not int or pid <= 1:
            raise ValueError("NOT_READY: owned process identity is invalid")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            if process_birth_identity(pid) == started.get("process_birth_identity"):
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    raise ValueError("NOT_READY: owned process cleanup is unconfirmed")
            # A different birth identity means this owned process already exited.
        unsigned = {k: v for k, v in started.items() if k != "receipt_digest"}
        unsigned.update(state="stopped", reason="owned_sut_stopped")
        raw = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        document = {
            **unsigned,
            "receipt_digest": "hmac-sha256:" + hmac.new(token, raw, hashlib.sha256).hexdigest(),
        }
        fd = os.open(stopped, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(json.dumps(document, sort_keys=True).encode())
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        (run / f"live-{started['instance_id']}.json").unlink(missing_ok=True)


def authenticate_managed_sut_readiness(
    selection: "ManagedSutReadinessSelectionV1", *, source_root: Path, secret_port: SecretPort
) -> None:
    """Verify independent authority and T3 receipts, then probe that owned process."""
    workspace = Path(selection.workspace_root)
    authenticate_managed_sut_receipts(
        workspace,
        selection.verification,
        secret_port=secret_port,
        authorization_scope_digest=selection.authorization_scope_digest,
        activity_receipt_digest=selection.activity_receipt_digest,
    )
    receipt = ManagedUserSutHost(source_root=source_root, secret_port=secret_port).preflight(
        workspace_root=workspace,
        receipt_path=workspace / selection.verification.managed_sut_start_receipt_ref.path,
        instance_id=selection.verification.sut_instance_id,
    )
    if (
        receipt.get("state") != "ready"
        or receipt.get("start_receipt_sha256")
        != "sha256:" + selection.verification.managed_sut_start_receipt_ref.digest
    ):
        raise InputError("managed SUT readiness receipt identity drifted")
