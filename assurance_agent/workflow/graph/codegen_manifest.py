"""Deterministically complete codegen generated-file manifests before freeze."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from assurance_agent.artifacts.models.codegen import (
    CodegenGeneratedFile,
    CodegenGeneratedFiles,
    CodegenGeneratedFilesSubmission,
    CodegenLayer,
)
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath, path_covers
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError

_LAYER_BY_TARGET: dict[str, CodegenLayer] = {
    "skill:aa-api-codegen": "api",
    "skill:aa-e2e-codegen": "e2e",
    "skill:aa-fuzz-codegen": "fuzz",
    "skill:aa-performance-codegen": "performance",
}


class CodegenManifestError(AaError):
    """An authored generated-file manifest cannot be safely completed."""


@dataclass(frozen=True)
class CodegenRepoFileReceipt:
    """Runtime observation bound to one manifest repo_path before freeze."""

    repo_path: str
    content_sha256: str


@dataclass(frozen=True)
class CodegenCompletionReceipt:
    """Runtime-only evidence needed to bind completion to frozen objects."""

    manifest_logical_path: str
    manifest_sha256: str
    files: tuple[CodegenRepoFileReceipt, ...]


def _close_fds_best_effort(fds: tuple[int | None, ...]) -> None:
    """Close every owned descriptor without letting cleanup mask the main result."""
    for fd in fds:
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


def _unlink_best_effort(filename: str, *, parent_fd: int) -> None:
    try:
        os.unlink(filename, dir_fd=parent_fd)
    except OSError:
        pass


def _read_regular_repo_file(
    project_root: Path,
    repo_path: str,
) -> tuple[Path, bytes, os.stat_result]:
    """Read one project-relative file without following any symlink component."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not directory:
        raise CodegenManifestError("platform cannot enforce no-follow repo file access")
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise CodegenManifestError(f"project root could not be resolved: {exc}") from exc

    parts = repo_path.split("/")
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        try:
            directory_fds.append(os.open(root, os.O_RDONLY | directory | no_follow))
            for part in parts[:-1]:
                directory_fds.append(
                    os.open(
                        part,
                        os.O_RDONLY | directory | no_follow,
                        dir_fd=directory_fds[-1],
                    )
                )
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | no_follow | nonblock,
                dir_fd=directory_fds[-1],
            )
        except FileNotFoundError as exc:
            raise CodegenManifestError(f"repo_path is missing: {repo_path}") from exc
        except OSError as exc:
            raise CodegenManifestError(
                f"repo_path could not be opened without following symlink components: {repo_path}: {exc}"
            ) from exc

        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise CodegenManifestError(f"repo_path must resolve to a regular file: {repo_path}")
        try:
            content = _read_open_file(file_fd)
            after = os.fstat(file_fd)
        except OSError as exc:
            raise CodegenManifestError(f"repo_path could not be read: {repo_path}: {exc}") from exc
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise CodegenManifestError(f"repo_path changed while it was read: {repo_path}")
        return root.joinpath(*parts), content, after
    finally:
        _close_fds_best_effort((file_fd, *reversed(directory_fds)))


def _open_manifest_no_follow(
    workspace: TaskWorkspace,
    filename: str,
) -> tuple[int, int, os.stat_result]:
    """Open the manifest and its parent beneath the task root without symlinks."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not directory:
        raise CodegenManifestError("platform cannot enforce no-follow manifest access")

    task_root = Path(os.path.abspath(workspace.root))
    change_dir = Path(os.path.abspath(workspace.change_dir))
    try:
        change_parts = change_dir.relative_to(task_root).parts
    except ValueError as exc:
        raise CodegenManifestError("codegen manifest path escapes the task workspace") from exc

    directory_fds: list[int] = []
    manifest_fd: int | None = None
    opened = False
    try:
        directory_fds.append(os.open(task_root, os.O_RDONLY | directory | no_follow))
        for part in (*change_parts, "codegen"):
            directory_fds.append(
                os.open(
                    part,
                    os.O_RDONLY | directory | no_follow,
                    dir_fd=directory_fds[-1],
                )
            )
        manifest_fd = os.open(
            filename,
            os.O_RDONLY | no_follow | nonblock,
            dir_fd=directory_fds[-1],
        )
        manifest_stat = os.fstat(manifest_fd)
        if not stat.S_ISREG(manifest_stat.st_mode):
            raise CodegenManifestError(f"codegen manifest must be a regular file: {filename}")
        opened = True
    except CodegenManifestError:
        raise
    except OSError as exc:
        raise CodegenManifestError(
            f"codegen manifest path must contain no symlink components: {filename}: {exc}"
        ) from exc
    finally:
        if not opened:
            _close_fds_best_effort((manifest_fd, *reversed(directory_fds)))

    parent_fd = directory_fds.pop()
    _close_fds_best_effort(tuple(reversed(directory_fds)))
    assert manifest_fd is not None
    return parent_fd, manifest_fd, manifest_stat


def _read_open_file(file_fd: int) -> bytes:
    os.lseek(file_fd, 0, os.SEEK_SET)
    duplicate_fd = os.dup(file_fd)
    try:
        stream = os.fdopen(duplicate_fd, "rb")
    except BaseException:
        _close_fds_best_effort((duplicate_fd,))
        raise
    with stream:
        return stream.read()


def _replace_open_manifest(
    *,
    parent_fd: int,
    filename: str,
    payload: bytes,
    mode: int,
) -> None:
    """Atomically replace the validated directory entry without mutating hardlinks."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    temp_name = f".{filename}.{uuid4().hex}.runtime.tmp"
    temp_fd: int | None = None
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
            stat.S_IMODE(mode),
            dir_fd=parent_fd,
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(temp_fd, remaining)
            if written == 0:
                raise OSError("zero-byte write while completing codegen manifest")
            remaining = remaining[written:]
        os.fsync(temp_fd)
        completed_fd = temp_fd
        temp_fd = None
        os.close(completed_fd)
        os.replace(
            temp_name,
            filename,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except OSError as exc:
        raise CodegenManifestError(f"codegen manifest could not be completed: {exc}") from exc
    finally:
        _close_fds_best_effort((temp_fd,))
        _unlink_best_effort(temp_name, parent_fd=parent_fd)


def complete_codegen_manifest(
    *,
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    claims: ResourceClaims | None = None,
) -> CodegenCompletionReceipt | None:
    """Replace non-authoritative submitted hashes with exact workspace bytes.

    Only the four primary codegen skills are completed. All validation and the
    rewrite happen before TreeStore.freeze_write_set, so the frozen output blob
    and its output digest describe the canonical, runtime-completed manifest.
    """
    layer = _LAYER_BY_TARGET.get(task.target)
    if layer is None:
        return None

    effective_claims = task.resources if claims is None else claims
    manifest_name = f"{layer}-generated-files.json"
    parent_fd, manifest_fd, manifest_stat = _open_manifest_no_follow(workspace, manifest_name)
    try:
        try:
            raw = json.loads(_read_open_file(manifest_fd).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodegenManifestError(
                f"codegen manifest is not readable JSON: {manifest_name}: {exc}"
            ) from exc

        try:
            submitted = CodegenGeneratedFilesSubmission.model_validate(raw)
        except ValidationError as exc:
            raise CodegenManifestError(f"codegen manifest failed authoring validation: {exc}") from exc
        if submitted.change_id != context.change_id:
            raise CodegenManifestError(
                f"codegen manifest change_id {submitted.change_id!r} must equal {context.change_id!r}"
            )
        if submitted.layer != layer:
            raise CodegenManifestError(
                f"codegen manifest layer {submitted.layer!r} must equal task layer {layer!r}"
            )

        repo_paths = tuple(item.repo_path for item in submitted.files)
        if len(repo_paths) != len(set(repo_paths)):
            raise CodegenManifestError("codegen manifest files repo_path values must be unique")

        completed_files: list[CodegenGeneratedFile] = []
        file_receipts: list[CodegenRepoFileReceipt] = []
        for item in sorted(submitted.files, key=lambda entry: entry.repo_path):
            logical_repo_path = ResourcePath.parse(f"repo:{item.repo_path}")
            if not any(
                path_covers(prefix, logical_repo_path) for prefix in effective_claims.authorization_writes
            ):
                raise CodegenManifestError(f"repo_path is outside authorization_writes: {item.repo_path}")
            _, content, repo_stat = _read_regular_repo_file(
                workspace.project_root,
                item.repo_path,
            )
            if (repo_stat.st_dev, repo_stat.st_ino) == (
                manifest_stat.st_dev,
                manifest_stat.st_ino,
            ):
                raise CodegenManifestError("codegen manifest must not list itself as a repo file")
            content_sha256 = hashlib.sha256(content).hexdigest()
            completed_files.append(
                CodegenGeneratedFile(
                    repo_path=item.repo_path,
                    disposition=item.disposition,
                    role=item.role,
                    case_ids=item.case_ids,
                    content_sha256=f"sha256:{content_sha256}",
                )
            )
            file_receipts.append(
                CodegenRepoFileReceipt(
                    repo_path=item.repo_path,
                    content_sha256=content_sha256,
                )
            )

        completed = CodegenGeneratedFiles(
            schema_version=submitted.schema_version,
            change_id=submitted.change_id,
            layer=submitted.layer,
            files=tuple(completed_files),
        )
        canonical = (
            json.dumps(
                completed.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        _replace_open_manifest(
            parent_fd=parent_fd,
            filename=manifest_name,
            payload=canonical,
            mode=manifest_stat.st_mode,
        )
        return CodegenCompletionReceipt(
            manifest_logical_path=f"change:codegen/{manifest_name}",
            manifest_sha256=hashlib.sha256(canonical).hexdigest(),
            files=tuple(file_receipts),
        )
    finally:
        _close_fds_best_effort((manifest_fd, parent_fd))


def verify_frozen_codegen_completion(
    *,
    store: TreeStore,
    write_set_id: str,
    receipt: CodegenCompletionReceipt,
) -> None:
    """Bind a pre-freeze completion receipt to content-addressed frozen bytes."""
    try:
        write_set = store.load_write_set(write_set_id)
        manifest_sha256 = write_set.outputs_sha256.get(receipt.manifest_logical_path)
        if manifest_sha256 != receipt.manifest_sha256:
            raise CodegenManifestError("codegen manifest changed after runtime completion")
        manifest_bytes = store.read_object(receipt.manifest_sha256)
        if hashlib.sha256(manifest_bytes).hexdigest() != receipt.manifest_sha256:
            raise CodegenManifestError("frozen codegen manifest digest does not match its receipt")

        for file_receipt in receipt.files:
            logical_path = f"repo:{file_receipt.repo_path}"
            frozen_bytes = store.read_frozen_write_set_bytes(write_set_id, logical_path)
            if hashlib.sha256(frozen_bytes).hexdigest() != file_receipt.content_sha256:
                raise CodegenManifestError(
                    f"repo_path changed after codegen manifest completion: {file_receipt.repo_path}"
                )
    except CodegenManifestError:
        raise
    except (OSError, WorkspaceError) as exc:
        raise CodegenManifestError(f"frozen codegen evidence could not be verified: {exc}") from exc


__all__ = [
    "CodegenCompletionReceipt",
    "CodegenManifestError",
    "CodegenRepoFileReceipt",
    "complete_codegen_manifest",
    "verify_frozen_codegen_completion",
]
