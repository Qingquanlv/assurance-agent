from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, TypeAlias, cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError

from assurance_product.change_workspace import (
    ChangeWorkspace,
    require_real_directory,
    safe_change_id,
)
from assurance_product.models import (
    ApplyManifestFileV1,
    ApplyManifestV1,
    PublishFileV1,
    PublishJournalRecordV1,
    PublishJournalV1,
    PublishReceiptV1,
    StatusV1,
)
from assurance_product.verification import authenticate_verified_delivery

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_RECEIPT_NAME = "publish-receipt.json"
_JOURNAL_NAME = "publish-journal.json"
_STATUS_NAME = "status.json"


class PublishError(GraphEngineError):
    """Raised when an achieved change cannot be published exactly."""


@dataclass(frozen=True, slots=True)
class _Bindings:
    change_id: str
    manifest_digest: str
    source_digest: str
    target_baseline: str
    temp_identity: str
    backup_identity: str
    final_digest: str


_PathIdentity: TypeAlias = tuple[int, int, int, int, int, int, int]


def _same_path_object(observed: _PathIdentity, expected: _PathIdentity) -> bool:
    return observed[:6] == expected[:6]


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    spec: ApplyManifestFileV1
    content: bytes
    temp_name: str
    backup_name: str
    already_matching: bool
    target_identity: _PathIdentity | None


def _journal_cut(phase: str) -> None:
    del phase


def _publication_cut(phase: str) -> None:
    del phase


def _metadata_cut(phase: str, name: str) -> None:
    del phase, name


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _lstat_at(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_authenticated_directory(path: Path, label: str) -> int:
    try:
        expected = _directory_identity(path.lstat())
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise PublishError(f"{label} is unavailable") from error
    try:
        if (
            _directory_identity(os.fstat(descriptor)) != expected
            or _directory_identity(path.lstat()) != expected
        ):
            raise PublishError(f"{label} changed during authentication")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_project_parent(project_directory: int, relative: str) -> int:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PublishError(f"invalid project-relative target: {relative}")
    current = os.dup(project_directory)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as error:
                raise PublishError(f"target parent contains unsafe ancestor: {relative}") from error
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise PublishError(f"target parent contains unsafe ancestor: {relative}")
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _require_project_parent(
    project_directory: int,
    relative: str,
    expected_directory: int,
) -> None:
    observed = _open_project_parent(project_directory, relative)
    try:
        if _directory_identity(os.fstat(observed)) != _directory_identity(os.fstat(expected_directory)):
            raise PublishError(f"target parent changed after authentication: {relative}")
    finally:
        os.close(observed)


def _acquire_export_lock(change_root: Path) -> int:
    """Serialize trusted product publishers; unrelated same-UID editors are outside this protocol."""
    directory = _open_authenticated_directory(change_root, "change publication directory")
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(".publish.lock", flags, 0o600, dir_fd=directory)
        try:
            _reject_irregular(os.fstat(descriptor), "publish lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PublishError("publish is already in progress for this change") from error
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as error:
        raise PublishError("publish lock is unsafe or unavailable") from error
    finally:
        os.close(directory)


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _replace_published_file(
    parent: Path,
    temporary_name: str,
    target_name: str,
    *,
    expected_target: _PathIdentity | None,
    expected_content: bytes,
    project_directory: int,
    target_relative: str,
) -> None:
    try:
        directory = _open_project_parent(project_directory, target_relative)
    except PublishError as error:
        raise PublishError("publish target parent is unavailable") from error
    try:
        try:
            temporary = os.open(
                temporary_name,
                _FILE_READ_FLAGS,
                dir_fd=directory,
            )
        except OSError as error:
            raise PublishError("publish temporary is unsafe or missing") from error
        try:
            temporary_state = _path_identity(os.fstat(temporary))
            _reject_irregular(os.fstat(temporary), temporary_name)
            if _read_descriptor(temporary) != expected_content:
                raise PublishError("publish temporary content does not match the manifest")
            if _path_identity(os.fstat(temporary)) != temporary_state:
                raise PublishError("publish temporary changed while reading")
            named_temporary = _lstat_at(directory, temporary_name)
            if named_temporary is None or _path_identity(named_temporary) != temporary_state:
                raise PublishError("publish temporary changed after authentication")
            _publication_cut("export-before-replace-authentication")
            _require_project_parent(project_directory, target_relative, directory)
            observed_target = _lstat_at(directory, target_name)
            observed_target_state = None if observed_target is None else _path_identity(observed_target)
            if observed_target_state != expected_target:
                raise PublishError("publish destination changed before replacement")
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            published = _lstat_at(directory, target_name)
            if published is None or not _same_path_object(_path_identity(published), temporary_state):
                raise PublishError("publish destination replacement is indeterminate")
        finally:
            os.close(temporary)
        os.fsync(directory)
    finally:
        os.close(directory)


def select_publish_change(project_root: Path, change_id: str | None) -> str:
    project = require_real_directory(Path(project_root).resolve())
    if change_id is not None:
        return safe_change_id(change_id)
    candidates = _unpublished_achieved(project)
    listing = ", ".join(candidates) if candidates else "(none)"
    if len(candidates) != 1:
        raise PublishError(
            f"export requires exactly one achieved unpublished change; found {len(candidates)}: {listing}"
        )
    return candidates[0]


def publish_achieved(project_root: Path, change_id: str) -> PublishReceiptV1:
    try:
        workspace = ChangeWorkspace.open(Path(project_root).resolve(), safe_change_id(change_id))
    except ValueError as error:
        raise PublishError(str(error)) from error
    project_directory = _open_authenticated_directory(workspace.paths.project_root, "project root")
    try:
        lock = _acquire_export_lock(workspace.paths.change_root)
        try:
            return _publish_achieved_locked(workspace, project_directory)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
    finally:
        os.close(project_directory)


def _publish_achieved_locked(
    workspace: ChangeWorkspace,
    project_directory: int,
) -> PublishReceiptV1:
    status = _read_status(workspace)
    if status.change.state != "achieved" or status.change.change_id != workspace.paths.change_root.name:
        raise PublishError("cannot publish a change that is not achieved")
    manifest = _read_manifest(workspace)
    if manifest.change_id != workspace.paths.change_root.name:
        raise PublishError("apply manifest change_id does not match the change")
    if status.execution_gate is not None:
        if status.quality_gate is None:
            if status.execution_gate.validation_profile is not None:
                raise PublishError("verified delivery quality gate is missing")
        else:
            try:
                authenticate_verified_delivery(
                    workspace.paths.project_root,
                    workspace.paths.change_root.name,
                    status.invocation_id,
                    execution_gate=status.execution_gate,
                    quality_gate=status.quality_gate,
                )
            except ValueError as error:
                raise PublishError(f"verified delivery authentication failed: {error}") from error
    planned = _plan_files(workspace, manifest)
    files = tuple(_publish_file(item) for item in planned)
    bindings = _bindings(manifest, files)
    existing = _read_optional_receipt(workspace)
    if (
        existing is not None
        and _receipt_matches(existing, bindings)
        and _targets_match_final(workspace, planned)
    ):
        _mark_published(workspace, status)
        _cleanup_transaction(workspace, planned)
        return existing
    journal = _read_optional_journal(workspace)
    last = journal.records[-1] if journal is not None and journal.records else None
    if last is not None and last.phase != "rolled_back" and not _record_matches(last, bindings):
        raise PublishError("publish journal does not match the current apply manifest")
    if last is not None and last.phase == "committed" and _targets_match_final(workspace, planned):
        receipt = _receipt(bindings, files)
        _write_receipt(workspace, receipt)
        _mark_published(workspace, status)
        _cleanup_transaction(workspace, planned)
        return receipt
    if last is None or last.phase == "rolled_back" or journal is None:
        prior: tuple[PublishJournalRecordV1, ...] = ()
    else:
        prior = journal.records
    return _complete_publication(
        workspace,
        status,
        planned,
        files,
        bindings,
        prior,
        project_directory,
    )


def _unpublished_achieved(project: Path) -> tuple[str, ...]:
    changes = project / "qa" / "changes"
    if changes.is_symlink() or not changes.is_dir():
        return ()
    found: list[str] = []
    for child in sorted(changes.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        path = child / _STATUS_NAME
        if path.is_symlink() or not path.is_file():
            continue
        try:
            status = StatusV1.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            continue
        if (
            status.change.state == "achieved"
            and status.publication.status == "ready"
            and status.change.change_id == child.name
        ):
            found.append(child.name)
    return tuple(found)


def _complete_publication(
    workspace: ChangeWorkspace,
    status: StatusV1,
    planned: tuple[_PlannedFile, ...],
    files: tuple[PublishFileV1, ...],
    bindings: _Bindings,
    prior: tuple[PublishJournalRecordV1, ...],
    project_directory: int,
) -> PublishReceiptV1:
    records = prior
    try:
        if not records or records[-1].phase == "rolled_back":
            _prepare_temps(workspace, planned)
            records = _append_journal(workspace, records, bindings, files, "prepared")
            _journal_cut("prepared")
        else:
            _prepare_temps(workspace, planned)
        if records[-1].phase == "prepared":
            records = _append_journal(workspace, records, bindings, files, "replacing")
            _journal_cut("replacing")
        if records[-1].phase == "replacing":
            _replace_targets(workspace, planned, project_directory)
            records = _append_journal(workspace, records, bindings, files, "committed")
            _journal_cut("committed")
    except PublishError:
        _rollback_and_raise(workspace, planned, files, bindings, records)
    except OSError as error:
        try:
            _rollback_and_raise(workspace, planned, files, bindings, records)
        except PublishError as publish_error:
            raise publish_error from error
    receipt = _receipt(bindings, files)
    _write_receipt(workspace, receipt)
    _mark_published(workspace, status)
    _cleanup_transaction(workspace, planned)
    return receipt


def _rollback_and_raise(
    workspace: ChangeWorkspace,
    planned: tuple[_PlannedFile, ...],
    files: tuple[PublishFileV1, ...],
    bindings: _Bindings,
    records: tuple[PublishJournalRecordV1, ...],
) -> NoReturn:
    restore_error: BaseException | None = None
    try:
        _rollback_targets(workspace, planned)
    except (OSError, PublishError) as error:
        restore_error = error
    _append_journal(workspace, records, bindings, files, "rolled_back")
    _journal_cut("rolled_back")
    _cleanup_transaction(workspace, planned)
    if restore_error is not None:
        raise PublishError("publish rolled back to the target baseline") from restore_error
    raise PublishError("publish rolled back to the target baseline")


def _plan_files(workspace: ChangeWorkspace, manifest: ApplyManifestV1) -> tuple[_PlannedFile, ...]:
    planned: list[_PlannedFile] = []
    for spec in sorted(manifest.files, key=lambda item: item.target_path):
        source = _project_file(workspace.paths.project_root, spec.source_path)
        content = _read_regular(source, spec.source_path)
        digest = _prefixed(content)
        if digest != spec.source_sha256:
            raise PublishError(f"source digest mismatch: {spec.target_path}")
        target = _project_file(workspace.paths.project_root, spec.target_path)
        already_matching, target_identity = _authenticate_target(target, spec)
        temp_name, backup_name = _transaction_names(manifest.change_id, spec.target_path, spec.source_sha256)
        planned.append(
            _PlannedFile(
                spec=spec,
                content=content,
                temp_name=temp_name,
                backup_name=backup_name,
                already_matching=already_matching,
                target_identity=target_identity,
            )
        )
    return tuple(planned)


def _authenticate_target(target: Path, spec: ApplyManifestFileV1) -> tuple[bool, _PathIdentity | None]:
    try:
        content, identity = _read_regular_with_identity(target, spec.target_path)
    except FileNotFoundError:
        if spec.baseline_sha256 is not None:
            raise PublishError(f"target baseline drift: {spec.target_path}")
        return False, None
    digest = _prefixed(content)
    if digest == spec.source_sha256:
        return True, identity
    if digest == spec.baseline_sha256:
        return False, identity
    raise PublishError(f"target baseline drift: {spec.target_path}")


def _prepare_temps(workspace: ChangeWorkspace, planned: tuple[_PlannedFile, ...]) -> None:
    for item in planned:
        if item.already_matching or _target_has_source(workspace, item):
            continue
        parent = _ensure_parent(workspace.paths.project_root, item.spec.target_path)
        _write_named(parent, item.temp_name, item.content, item.spec.mode)
        if item.spec.baseline_sha256 is None:
            continue
        target = parent / _leaf(item.spec.target_path)
        if not target.exists():
            continue
        if _prefixed(_read_regular(target, item.spec.target_path)) != item.spec.baseline_sha256:
            continue
        _write_named(
            parent,
            item.backup_name,
            target.read_bytes(),
            stat.S_IMODE(target.stat().st_mode),
        )


def _replace_targets(
    workspace: ChangeWorkspace,
    planned: tuple[_PlannedFile, ...],
    project_directory: int,
) -> None:
    for item in planned:
        parent_directory = _open_project_parent(project_directory, item.spec.target_path)
        try:
            target = _project_file(workspace.paths.project_root, item.spec.target_path)
            _require_path_identity(target, item.target_identity, item.spec.target_path)
            if item.already_matching or _target_has_source(workspace, item):
                os.fsync(parent_directory)
                continue
        finally:
            os.close(parent_directory)
        parent = _ensure_parent(workspace.paths.project_root, item.spec.target_path)
        target_name = _leaf(item.spec.target_path)
        temp = parent / item.temp_name
        if not temp.is_file():
            _write_named(parent, item.temp_name, item.content, item.spec.mode)
        _replace_published_file(
            parent,
            item.temp_name,
            target_name,
            expected_target=item.target_identity,
            expected_content=item.content,
            project_directory=project_directory,
            target_relative=item.spec.target_path,
        )


def _rollback_targets(workspace: ChangeWorkspace, planned: tuple[_PlannedFile, ...]) -> None:
    for item in reversed(planned):
        if item.already_matching:
            continue
        parent = _project_file(workspace.paths.project_root, item.spec.target_path).parent
        target = parent / _leaf(item.spec.target_path)
        try:
            current = _prefixed(_read_regular(target, item.spec.target_path))
        except FileNotFoundError:
            current = None
        except PublishError:
            current = None
        if current == item.spec.source_sha256:
            if item.spec.baseline_sha256 is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                continue
            backup = parent / item.backup_name
            if not backup.is_file():
                raise PublishError(f"publish rollback backup is missing: {item.spec.target_path}")
            os.replace(backup, target)
            directory = os.open(parent, _DIRECTORY_FLAGS)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        elif current not in {item.spec.baseline_sha256, None}:
            raise PublishError(f"publish rollback encountered target drift: {item.spec.target_path}")


def _target_has_source(workspace: ChangeWorkspace, item: _PlannedFile) -> bool:
    target = _project_file(workspace.paths.project_root, item.spec.target_path)
    try:
        return _prefixed(_read_regular(target, item.spec.target_path)) == item.spec.source_sha256
    except (FileNotFoundError, PublishError):
        return False


def _targets_match_final(workspace: ChangeWorkspace, planned: tuple[_PlannedFile, ...]) -> bool:
    return all(_target_has_source(workspace, item) for item in planned)


def _cleanup_transaction(workspace: ChangeWorkspace, planned: tuple[_PlannedFile, ...]) -> None:
    for item in planned:
        parent = _project_file(workspace.paths.project_root, item.spec.target_path).parent
        for name in (item.temp_name, item.backup_name):
            path = parent / name
            try:
                path.unlink()
            except FileNotFoundError:
                continue


def _publish_file(item: _PlannedFile) -> PublishFileV1:
    return PublishFileV1(
        target_path=item.spec.target_path,
        source_path=item.spec.source_path,
        source_sha256=item.spec.source_sha256,
        baseline_sha256=item.spec.baseline_sha256,
        final_sha256=item.spec.source_sha256,
        temp_name=item.temp_name,
        backup_name=item.backup_name,
    )


def _bindings(manifest: ApplyManifestV1, files: tuple[PublishFileV1, ...]) -> _Bindings:
    source_digest = canonical_digest({item.target_path: item.source_sha256 for item in files})
    return _Bindings(
        change_id=manifest.change_id,
        manifest_digest=manifest.digest,
        source_digest=source_digest,
        target_baseline=canonical_digest({item.target_path: item.baseline_sha256 for item in files}),
        temp_identity=canonical_digest({item.target_path: item.temp_name for item in files}),
        backup_identity=canonical_digest({item.target_path: item.backup_name for item in files}),
        final_digest=source_digest,
    )


def _receipt(bindings: _Bindings, files: tuple[PublishFileV1, ...]) -> PublishReceiptV1:
    return PublishReceiptV1(
        schema_version="1",
        change_id=bindings.change_id,
        manifest_digest=bindings.manifest_digest,
        source_digest=bindings.source_digest,
        target_baseline=bindings.target_baseline,
        final_digest=bindings.final_digest,
        files=files,
    )


def _record(
    bindings: _Bindings,
    files: tuple[PublishFileV1, ...],
    phase: Literal["prepared", "replacing", "committed", "rolled_back"],
) -> PublishJournalRecordV1:
    return PublishJournalRecordV1(
        phase=phase,
        change_id=bindings.change_id,
        manifest_digest=bindings.manifest_digest,
        source_digest=bindings.source_digest,
        target_baseline=bindings.target_baseline,
        temp_identity=bindings.temp_identity,
        backup_identity=bindings.backup_identity,
        final_digest=bindings.final_digest,
        files=files,
    )


def _append_journal(
    workspace: ChangeWorkspace,
    records: tuple[PublishJournalRecordV1, ...],
    bindings: _Bindings,
    files: tuple[PublishFileV1, ...],
    phase: Literal["prepared", "replacing", "committed", "rolled_back"],
) -> tuple[PublishJournalRecordV1, ...]:
    updated = (*records, _record(bindings, files, phase))
    _write_json(
        workspace.paths.change_root / _JOURNAL_NAME,
        PublishJournalV1(schema_version="1", records=updated).model_dump(mode="json"),
    )
    return updated


def _receipt_matches(receipt: PublishReceiptV1, bindings: _Bindings) -> bool:
    return (
        receipt.change_id == bindings.change_id
        and receipt.manifest_digest == bindings.manifest_digest
        and receipt.source_digest == bindings.source_digest
        and receipt.target_baseline == bindings.target_baseline
        and receipt.final_digest == bindings.final_digest
    )


def _record_matches(record: PublishJournalRecordV1, bindings: _Bindings) -> bool:
    return (
        record.change_id == bindings.change_id
        and record.manifest_digest == bindings.manifest_digest
        and record.source_digest == bindings.source_digest
        and record.target_baseline == bindings.target_baseline
        and record.temp_identity == bindings.temp_identity
        and record.backup_identity == bindings.backup_identity
        and record.final_digest == bindings.final_digest
    )


def _read_status(workspace: ChangeWorkspace) -> StatusV1:
    path = workspace.paths.change_root / _STATUS_NAME
    try:
        return StatusV1.model_validate_json(_read_regular(path, _STATUS_NAME))
    except FileNotFoundError as error:
        raise PublishError("achieved status is missing") from error
    except (OSError, ValueError) as error:
        raise PublishError("achieved status is invalid") from error


def _read_manifest(workspace: ChangeWorkspace) -> ApplyManifestV1:
    path = workspace.paths.apply_manifest
    try:
        return ApplyManifestV1.model_validate_json(_read_regular(path, "apply-manifest.json"))
    except FileNotFoundError as error:
        raise PublishError("apply manifest is missing") from error
    except (OSError, ValueError) as error:
        raise PublishError("apply manifest is invalid") from error


def _read_optional_receipt(workspace: ChangeWorkspace) -> PublishReceiptV1 | None:
    path = workspace.paths.change_root / _RECEIPT_NAME
    if not path.exists():
        return None
    try:
        return PublishReceiptV1.model_validate_json(_read_regular(path, _RECEIPT_NAME))
    except (OSError, ValueError) as error:
        raise PublishError("publish receipt is invalid") from error


def _read_optional_journal(workspace: ChangeWorkspace) -> PublishJournalV1 | None:
    path = workspace.paths.change_root / _JOURNAL_NAME
    if not path.exists():
        return None
    try:
        return PublishJournalV1.model_validate_json(_read_regular(path, _JOURNAL_NAME))
    except (OSError, ValueError) as error:
        raise PublishError("publish journal is invalid") from error


def _write_receipt(workspace: ChangeWorkspace, receipt: PublishReceiptV1) -> None:
    _write_json(workspace.paths.change_root / _RECEIPT_NAME, receipt.model_dump(mode="json"))


def _mark_published(workspace: ChangeWorkspace, status: StatusV1) -> None:
    if status.publication.status == "published":
        _fsync_directory(workspace.paths.change_root)
        return
    payload = status.model_dump(mode="json")
    payload["publication"] = {"status": "published"}
    _write_json(
        workspace.paths.change_root / _STATUS_NAME, StatusV1.model_validate(payload).model_dump(mode="json")
    )


def _write_json(path: Path, payload: object) -> None:
    content = canonical_json_bytes(cast(JSONValue, payload)) + b"\n"
    temporary_name = f".{path.name}.tmp"
    directory = _open_authenticated_directory(path.parent, f"{path.name} parent")
    descriptor: int | None = None
    installed = False
    cleanup_allowed = False
    try:
        destination = _lstat_at(directory, path.name)
        if destination is not None:
            _reject_irregular(destination, path.name)
            mode = stat.S_IMODE(destination.st_mode)
        else:
            mode = 0o644
        try:
            descriptor = os.open(temporary_name, _FILE_WRITE_FLAGS, mode, dir_fd=directory)
        except FileExistsError:
            descriptor = os.open(
                temporary_name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            _reject_irregular(os.fstat(descriptor), temporary_name)
        cleanup_allowed = True
        os.ftruncate(descriptor, 0)
        os.fchmod(descriptor, mode)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise PublishError(f"failed to write {temporary_name}")
            view = view[written:]
        os.fsync(descriptor)
        temporary_identity = _path_identity(os.fstat(descriptor))
        named_temporary = _lstat_at(directory, temporary_name)
        if named_temporary is None or not _same_path_object(
            _path_identity(named_temporary), temporary_identity
        ):
            raise PublishError(f"{temporary_name} changed during publication")
        _metadata_cut("after-file-fsync", path.name)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        installed = True
        published = _lstat_at(directory, path.name)
        if published is None or not _same_path_object(_path_identity(published), temporary_identity):
            raise PublishError(f"{path.name} replacement is indeterminate")
        _metadata_cut("after-replace", path.name)
        os.fsync(directory)
    except BaseException:
        if descriptor is not None and cleanup_allowed and not installed:
            try:
                opened_identity = _path_identity(os.fstat(descriptor))
                named_temporary = _lstat_at(directory, temporary_name)
                if named_temporary is not None and _same_path_object(
                    _path_identity(named_temporary), opened_identity
                ):
                    os.unlink(temporary_name, dir_fd=directory)
                    os.fsync(directory)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _fsync_directory(path: Path) -> None:
    directory = _open_authenticated_directory(path, f"{path.name} directory")
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _transaction_names(change_id: str, target_path: str, source_sha256: str) -> tuple[str, str]:
    token = canonical_digest(
        {"change_id": change_id, "target_path": target_path, "source_sha256": source_sha256}
    )[:24]
    name = _leaf(target_path)
    return f".{name}.{token}.tmp", f".{name}.{token}.bak"


def _project_file(project: Path, relative: str) -> Path:
    path = project.joinpath(*PurePosixPath(relative).parts)
    try:
        path.parent.resolve().relative_to(project.resolve())
    except ValueError as error:
        raise PublishError(f"path escapes the project: {relative}") from error
    return path


def _ensure_parent(project: Path, relative: str) -> Path:
    parent = _project_file(project, relative).parent
    parent.mkdir(parents=True, exist_ok=True)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PublishError(f"target parent is not a real directory: {relative}")
    return parent


def _write_named(parent: Path, name: str, content: bytes, mode: int) -> None:
    path = parent / name
    try:
        existing = _read_regular(path, name)
    except FileNotFoundError:
        descriptor = os.open(path, _FILE_WRITE_FLAGS, mode)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise PublishError(f"failed to write {name}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    if existing != content:
        raise PublishError(f"publish transaction file conflicts: {name}")


def _read_regular(path: Path, label: str) -> bytes:
    content, _identity = _read_regular_with_identity(path, label)
    return content


def _read_regular_with_identity(path: Path, label: str) -> tuple[bytes, _PathIdentity]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    _reject_irregular(info, label)
    expected = _path_identity(info)
    try:
        descriptor = os.open(path, _FILE_READ_FLAGS)
    except OSError as error:
        raise PublishError(f"failed to open regular file: {label}") from error
    try:
        opened = os.fstat(descriptor)
        _reject_irregular(opened, label)
        if _path_identity(opened) != expected:
            raise PublishError(f"path changed during authentication: {label}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        if _path_identity(os.fstat(descriptor)) != expected:
            raise PublishError(f"path changed during authentication: {label}")
    finally:
        os.close(descriptor)
    try:
        final = path.lstat()
    except FileNotFoundError as error:
        raise PublishError(f"path changed during authentication: {label}") from error
    if _path_identity(final) != expected:
        raise PublishError(f"path changed during authentication: {label}")
    return b"".join(chunks), expected


def _require_path_identity(path: Path, expected: _PathIdentity | None, label: str) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
        raise PublishError(f"target changed after authentication: {label}") from None
    if expected is None or _path_identity(observed) != expected:
        raise PublishError(f"target changed after authentication: {label}")


def _path_identity(info: os.stat_result) -> _PathIdentity:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _reject_irregular(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise PublishError(f"symlink is not allowed: {label}")
    if not stat.S_ISREG(info.st_mode):
        raise PublishError(f"path is not a regular file: {label}")
    if info.st_nlink != 1:
        raise PublishError(f"hard link is not allowed: {label}")


def _prefixed(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _leaf(relative: str) -> str:
    return relative.rsplit("/", 1)[-1]


__all__ = [
    "PublishError",
    "publish_achieved",
    "select_publish_change",
]
