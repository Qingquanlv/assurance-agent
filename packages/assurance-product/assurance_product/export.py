from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, cast

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

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
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


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    spec: ApplyManifestFileV1
    content: bytes
    temp_name: str
    backup_name: str
    already_matching: bool


def _journal_cut(phase: str) -> None:
    del phase


def _replace_published_file(parent: Path, temporary_name: str, target_name: str) -> None:
    os.replace(parent / temporary_name, parent / target_name)
    directory = os.open(parent, _DIRECTORY_FLAGS)
    try:
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
    status = _read_status(workspace)
    if status.change.state != "achieved" or status.change.change_id != workspace.paths.change_root.name:
        raise PublishError("cannot publish a change that is not achieved")
    manifest = _read_manifest(workspace)
    if manifest.change_id != workspace.paths.change_root.name:
        raise PublishError("apply manifest change_id does not match the change")
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
    return _complete_publication(workspace, status, planned, files, bindings, prior)


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
            _replace_targets(workspace, planned)
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
        already_matching = _authenticate_target(target, spec)
        temp_name, backup_name = _transaction_names(manifest.change_id, spec.target_path, spec.source_sha256)
        planned.append(
            _PlannedFile(
                spec=spec,
                content=content,
                temp_name=temp_name,
                backup_name=backup_name,
                already_matching=already_matching,
            )
        )
    return tuple(planned)


def _authenticate_target(target: Path, spec: ApplyManifestFileV1) -> bool:
    try:
        info = target.lstat()
    except FileNotFoundError:
        if spec.baseline_sha256 is not None:
            raise PublishError(f"target baseline drift: {spec.target_path}")
        return False
    _reject_irregular(info, spec.target_path)
    digest = _prefixed(_read_regular(target, spec.target_path))
    if digest == spec.source_sha256:
        return True
    if digest == spec.baseline_sha256:
        return False
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


def _replace_targets(workspace: ChangeWorkspace, planned: tuple[_PlannedFile, ...]) -> None:
    for item in planned:
        if item.already_matching or _target_has_source(workspace, item):
            continue
        parent = _ensure_parent(workspace.paths.project_root, item.spec.target_path)
        target_name = _leaf(item.spec.target_path)
        temp = parent / item.temp_name
        if not temp.is_file():
            _write_named(parent, item.temp_name, item.content, item.spec.mode)
        _replace_published_file(parent, item.temp_name, target_name)


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
        return
    payload = status.model_dump(mode="json")
    payload["publication"] = {"status": "published"}
    _write_json(
        workspace.paths.change_root / _STATUS_NAME, StatusV1.model_validate(payload).model_dump(mode="json")
    )


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json_bytes(cast(JSONValue, payload)) + b"\n")
    os.replace(temporary, path)


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
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    _reject_irregular(info, label)
    return path.read_bytes()


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
