from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

import pytest

from tests.product.test_result_export import CHANGE_ID, TARGET_A, TARGET_B, write_achieved

_PHASES = ("prepared", "replacing", "committed")


def test_retry_after_replace_crash_syncs_matching_target_parent_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(
        tmp_path,
        files=((TARGET_A, b"generated-a\n", b"original-a\n"),),
    )
    target = project / TARGET_A
    target_parent = target.parent.stat()
    real_fsync = export_mod.os.fsync

    def crash_before_target_directory_fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (
            stat.S_ISDIR(info.st_mode)
            and info.st_dev == target_parent.st_dev
            and info.st_ino == target_parent.st_ino
            and target.read_bytes() == b"generated-a\n"
        ):
            os._exit(92)
        real_fsync(descriptor)

    monkeypatch.setattr(export_mod.os, "fsync", crash_before_target_directory_fsync)
    process_id = os.fork()
    if process_id == 0:
        export_mod.publish_achieved(project, CHANGE_ID)
        os._exit(90)
    _child, status = os.waitpid(process_id, 0)
    assert os.waitstatus_to_exitcode(status) == 92

    journal_path = project / "qa" / "changes" / CHANGE_ID / "publish-journal.json"
    journal = PublishJournalV1.model_validate_json(journal_path.read_bytes())
    assert journal.records[-1].phase == "replacing"
    assert target.read_bytes() == b"generated-a\n"
    assert not (journal_path.parent / "publish-receipt.json").exists()

    synced_target_parents = 0

    def count_target_directory_fsync(descriptor: int) -> None:
        nonlocal synced_target_parents
        info = os.fstat(descriptor)
        if (
            stat.S_ISDIR(info.st_mode)
            and info.st_dev == target_parent.st_dev
            and info.st_ino == target_parent.st_ino
        ):
            synced_target_parents += 1
        real_fsync(descriptor)

    real_append_journal = export_mod._append_journal

    def require_sync_before_commit(*args: Any, **kwargs: Any) -> Any:
        phase = args[4]
        if phase == "committed":
            assert synced_target_parents >= 1
        return real_append_journal(*args, **kwargs)

    monkeypatch.setattr(export_mod.os, "fsync", count_target_directory_fsync)
    monkeypatch.setattr(export_mod, "_append_journal", require_sync_before_commit)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)

    assert receipt.change_id == CHANGE_ID
    assert synced_target_parents >= 1


def test_metadata_publication_fsyncs_file_before_replace_and_parent_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod

    project = write_achieved(tmp_path)
    change_root = project / "qa" / "changes" / CHANGE_ID
    change_root_identity = (change_root.stat().st_dev, change_root.stat().st_ino)
    metadata_names = {"publish-journal.json", "publish-receipt.json", "status.json"}
    events: list[tuple[str, str | int]] = []
    real_fsync = export_mod.os.fsync
    real_replace = export_mod.os.replace

    def record_fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            events.append(("file-fsync", info.st_ino))
        elif stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == change_root_identity:
            events.append(("parent-fsync", info.st_ino))
        real_fsync(descriptor)

    def record_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        destination_name = Path(os.fsdecode(destination)).name
        if destination_name in metadata_names:
            source_info = os.stat(
                source,
                dir_fd=kwargs.get("src_dir_fd"),
                follow_symlinks=False,
            )
            events.append((f"replace:{destination_name}", source_info.st_ino))
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr(export_mod.os, "fsync", record_fsync)
    monkeypatch.setattr(export_mod.os, "replace", record_replace)
    export_mod.publish_achieved(project, CHANGE_ID)

    replacements = [
        (index, event, inode) for index, (event, inode) in enumerate(events) if event.startswith("replace:")
    ]
    assert {event.removeprefix("replace:") for _index, event, _inode in replacements} == metadata_names
    for replacement_index, _event, inode in replacements:
        assert ("file-fsync", inode) in events[:replacement_index]
        next_replacement = next(
            (
                index
                for index, (event, _inode) in enumerate(
                    events[replacement_index + 1 :], replacement_index + 1
                )
                if event.startswith("replace:")
            ),
            len(events),
        )
        assert any(
            event == "parent-fsync" for event, _inode in events[replacement_index + 1 : next_replacement]
        )


@pytest.mark.parametrize(
    "metadata_name",
    ("publish-journal.json", "publish-receipt.json", "status.json"),
)
def test_metadata_replace_crash_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_name: str,
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1, StatusV1

    project = write_achieved(tmp_path, project=tmp_path / metadata_name.removesuffix(".json"))
    change_root = project / "qa" / "changes" / CHANGE_ID
    real_replace = export_mod.os.replace

    def crash_after_metadata_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        real_replace(source, destination, **kwargs)
        if Path(os.fsdecode(destination)).name == metadata_name:
            os._exit(93)

    monkeypatch.setattr(export_mod.os, "replace", crash_after_metadata_replace)
    process_id = os.fork()
    if process_id == 0:
        export_mod.publish_achieved(project, CHANGE_ID)
        os._exit(90)
    _child, status = os.waitpid(process_id, 0)
    assert os.waitstatus_to_exitcode(status) == 93

    change_root_identity = (change_root.stat().st_dev, change_root.stat().st_ino)
    synced_change_root = 0
    real_fsync = export_mod.os.fsync

    def count_change_root_fsync(descriptor: int) -> None:
        nonlocal synced_change_root
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == change_root_identity:
            synced_change_root += 1
        real_fsync(descriptor)

    monkeypatch.setattr(export_mod.os, "replace", real_replace)
    monkeypatch.setattr(export_mod.os, "fsync", count_change_root_fsync)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)

    journal = PublishJournalV1.model_validate_json((change_root / "publish-journal.json").read_bytes())
    published_status = StatusV1.model_validate_json((change_root / "status.json").read_bytes())
    assert journal.records[-1].phase == "committed"
    assert published_status.publication.status == "published"
    assert receipt.change_id == CHANGE_ID
    assert synced_change_root >= 1


@pytest.mark.parametrize(
    "metadata_name",
    ("publish-journal.json", "publish-receipt.json", "status.json"),
)
def test_metadata_file_fsync_exception_cleans_temporary_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_name: str,
) -> None:
    from assurance_product import export as export_mod

    project = write_achieved(tmp_path, project=tmp_path / f"exception-{metadata_name}")
    change_root = project / "qa" / "changes" / CHANGE_ID
    injected = False

    def fail_once_after_file_fsync(phase: str, written_name: str) -> None:
        nonlocal injected
        if phase == "after-file-fsync" and written_name == metadata_name and not injected:
            injected = True
            raise OSError("forced metadata publication failure")

    monkeypatch.setattr(export_mod, "_metadata_cut", fail_once_after_file_fsync, raising=False)
    with pytest.raises((OSError, export_mod.PublishError)):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert injected
    assert not (change_root / f".{metadata_name}.tmp").exists()

    monkeypatch.setattr(export_mod, "_metadata_cut", lambda _phase, _name: None, raising=False)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)
    assert receipt.change_id == CHANGE_ID


@pytest.mark.parametrize("phase", _PHASES)
def test_crash_after_journal_phase_then_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(tmp_path)

    def crash(name: str) -> None:
        if name == phase:
            raise RuntimeError(f"crash after {name}")

    monkeypatch.setattr(export_mod, "_journal_cut", crash)
    with pytest.raises(RuntimeError, match=phase):
        export_mod.publish_achieved(project, CHANGE_ID)

    journal = PublishJournalV1.model_validate_json(
        (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
    )
    assert journal.records[-1].phase == phase
    record = journal.records[-1]
    assert record.change_id == CHANGE_ID
    assert record.manifest_digest
    assert record.source_digest
    assert record.target_baseline
    assert record.temp_identity
    assert record.backup_identity
    assert record.final_digest
    if phase != "committed":
        assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()

    monkeypatch.setattr(export_mod, "_journal_cut", lambda _name: None)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"
    assert receipt.final_digest == receipt.source_digest
    assert (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").is_file()
    finished = PublishJournalV1.model_validate_json(
        (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
    )
    assert finished.records[-1].phase == "committed"
    assert {item.phase for item in finished.records} <= {"prepared", "replacing", "committed", "rolled_back"}


def test_failed_replace_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(tmp_path)
    calls = {"count": 0}

    def boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("forced replace failure")

    monkeypatch.setattr(export_mod, "_replace_published_file", boom)
    with pytest.raises(export_mod.PublishError):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()
    journal = PublishJournalV1.model_validate_json(
        (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
    )
    assert journal.records[-1].phase == "rolled_back"
    assert journal.records[-1].change_id == CHANGE_ID
    assert journal.records[-1].manifest_digest
    assert journal.records[-1].source_digest
    assert journal.records[-1].target_baseline
    assert journal.records[-1].temp_identity
    assert journal.records[-1].backup_identity
    assert journal.records[-1].final_digest


def test_export_directory_fsync_failure_rolls_back_and_retry_matches_uninterrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    uninterrupted = write_achieved(tmp_path, project=tmp_path / "uninterrupted")
    expected_receipt = export_mod.publish_achieved(uninterrupted, CHANGE_ID)
    expected_receipt_bytes = (
        uninterrupted / "qa" / "changes" / CHANGE_ID / "publish-receipt.json"
    ).read_bytes()
    expected_journal_bytes = (
        uninterrupted / "qa" / "changes" / CHANGE_ID / "publish-journal.json"
    ).read_bytes()

    project = write_achieved(tmp_path, project=tmp_path / "faulted")
    target_directory = (project / TARGET_A).parent.stat()
    real_fsync = export_mod.os.fsync
    injected: list[int] = []

    def fail_first_target_directory_fsync(descriptor: int) -> None:
        descriptor_info = os.fstat(descriptor)
        is_target_directory = (
            stat.S_ISDIR(descriptor_info.st_mode)
            and descriptor_info.st_dev == target_directory.st_dev
            and descriptor_info.st_ino == target_directory.st_ino
        )
        if is_target_directory and not injected:
            injected.append(descriptor)
            raise OSError("forced target directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(export_mod.os, "fsync", fail_first_target_directory_fsync)
    with pytest.raises(export_mod.PublishError, match="rolled back"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert len(injected) == 1
    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    receipt_path = project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json"
    journal_path = project / "qa" / "changes" / CHANGE_ID / "publish-journal.json"
    assert not receipt_path.exists()
    rolled_back = PublishJournalV1.model_validate_json(journal_path.read_bytes())
    assert rolled_back.records[-1].phase == "rolled_back"
    for item in rolled_back.records[-1].files:
        parent = project.joinpath(*item.target_path.split("/")).parent
        assert not (parent / item.temp_name).exists()
        assert not (parent / item.backup_name).exists()

    monkeypatch.setattr(export_mod.os, "fsync", real_fsync)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == (uninterrupted / TARGET_A).read_bytes()
    assert (project / TARGET_B).read_bytes() == (uninterrupted / TARGET_B).read_bytes()
    assert receipt == expected_receipt
    assert receipt_path.read_bytes() == expected_receipt_bytes
    assert journal_path.read_bytes() == expected_journal_bytes
    finished = PublishJournalV1.model_validate_json(journal_path.read_bytes())
    assert finished.records[-1].phase == "committed"
    for item in finished.records[-1].files:
        parent = project.joinpath(*item.target_path.split("/")).parent
        assert not (parent / item.temp_name).exists()
        assert not (parent / item.backup_name).exists()


@pytest.mark.parametrize("fault", ("write", "file-fsync"))
def test_export_file_write_or_file_fsync_failure_rolls_back_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    uninterrupted = write_achieved(tmp_path, project=tmp_path / "uninterrupted-file-fault")
    expected_receipt = export_mod.publish_achieved(uninterrupted, CHANGE_ID)
    expected_receipt_bytes = (
        uninterrupted / "qa" / "changes" / CHANGE_ID / "publish-receipt.json"
    ).read_bytes()
    expected_journal_bytes = (
        uninterrupted / "qa" / "changes" / CHANGE_ID / "publish-journal.json"
    ).read_bytes()

    project = write_achieved(tmp_path, project=tmp_path / f"faulted-{fault}")
    real_write = export_mod.os.write
    real_fsync = export_mod.os.fsync
    injected: list[int] = []

    def fail_first_regular_write(descriptor: int, content: bytes) -> int:
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and not injected:
            injected.append(descriptor)
            raise OSError("forced export temporary write failure")
        return real_write(descriptor, content)

    def fail_first_regular_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and not injected:
            injected.append(descriptor)
            raise OSError("forced export temporary file fsync failure")
        real_fsync(descriptor)

    if fault == "write":
        monkeypatch.setattr(export_mod.os, "write", fail_first_regular_write)
    else:
        monkeypatch.setattr(export_mod.os, "fsync", fail_first_regular_fsync)

    with pytest.raises(export_mod.PublishError, match="rolled back"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert len(injected) == 1
    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    receipt_path = project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json"
    journal_path = project / "qa" / "changes" / CHANGE_ID / "publish-journal.json"
    assert not receipt_path.exists()
    rolled_back = PublishJournalV1.model_validate_json(journal_path.read_bytes())
    assert rolled_back.records[-1].phase == "rolled_back"
    for item in rolled_back.records[-1].files:
        parent = project.joinpath(*item.target_path.split("/")).parent
        assert not (parent / item.temp_name).exists()
        assert not (parent / item.backup_name).exists()

    monkeypatch.setattr(export_mod.os, "write", real_write)
    monkeypatch.setattr(export_mod.os, "fsync", real_fsync)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)

    assert receipt == expected_receipt
    assert (project / TARGET_A).read_bytes() == (uninterrupted / TARGET_A).read_bytes()
    assert (project / TARGET_B).read_bytes() == (uninterrupted / TARGET_B).read_bytes()
    assert receipt_path.read_bytes() == expected_receipt_bytes
    assert journal_path.read_bytes() == expected_journal_bytes
    finished = PublishJournalV1.model_validate_json(journal_path.read_bytes())
    assert finished.records[-1].phase == "committed"
    for item in finished.records[-1].files:
        parent = project.joinpath(*item.target_path.split("/")).parent
        assert not (parent / item.temp_name).exists()
        assert not (parent / item.backup_name).exists()


def test_crash_after_rolled_back_leaves_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_product import export as export_mod

    project = write_achieved(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("forced replace failure")

    def crash(phase: str) -> None:
        if phase == "rolled_back":
            raise RuntimeError("crash after rolled_back")

    original_replace = export_mod._replace_published_file
    monkeypatch.setattr(export_mod, "_replace_published_file", boom)
    monkeypatch.setattr(export_mod, "_journal_cut", crash)
    with pytest.raises(RuntimeError, match="rolled_back"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()

    monkeypatch.setattr(export_mod, "_replace_published_file", original_replace)
    monkeypatch.setattr(export_mod, "_journal_cut", lambda _phase: None)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert receipt.change_id == CHANGE_ID


def test_mixed_already_matching_replace_failure_leaves_matching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    files = (
        (TARGET_A, b"generated-a\n", None),
        (TARGET_B, b"generated-b\n", b"original-b\n"),
    )
    project = write_achieved(tmp_path, files=files)
    already = project / TARGET_A
    already.parent.mkdir(parents=True, exist_ok=True)
    already.write_bytes(b"generated-a\n")

    def boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("forced replace failure")

    monkeypatch.setattr(export_mod, "_replace_published_file", boom)
    with pytest.raises(export_mod.PublishError):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert already.is_file()
    assert already.read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()
    journal = PublishJournalV1.model_validate_json(
        (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
    )
    assert journal.records[-1].phase == "rolled_back"


def test_restore_failure_after_replace_still_journals_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(tmp_path)
    original = export_mod._replace_published_file
    calls = {"count": 0}

    def boom(
        parent: Path,
        temporary_name: str,
        target_name: str,
        **kwargs: Any,
    ) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            original(parent, temporary_name, target_name, **kwargs)
            return
        for leftover in project.rglob("*.bak"):
            leftover.unlink()
        raise OSError("forced replace failure")

    monkeypatch.setattr(export_mod, "_replace_published_file", boom)
    with pytest.raises(export_mod.PublishError):
        export_mod.publish_achieved(project, CHANGE_ID)

    journal = PublishJournalV1.model_validate_json(
        (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
    )
    assert journal.records[-1].phase == "rolled_back"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_journal_transaction_identity_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_engine.canonical import canonical_json_bytes

    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(tmp_path)

    def crash(name: str) -> None:
        if name == "prepared":
            raise RuntimeError("crash after prepared")

    monkeypatch.setattr(export_mod, "_journal_cut", crash)
    with pytest.raises(RuntimeError, match="prepared"):
        export_mod.publish_achieved(project, CHANGE_ID)

    path = project / "qa" / "changes" / CHANGE_ID / "publish-journal.json"
    journal = PublishJournalV1.model_validate_json(path.read_bytes())
    record = journal.records[-1]
    mutated = record.model_copy(update={"temp_identity": "0" * 64, "backup_identity": "1" * 64})
    path.write_bytes(
        canonical_json_bytes(PublishJournalV1(schema_version="1", records=(mutated,)).model_dump(mode="json"))
        + b"\n"
    )

    monkeypatch.setattr(export_mod, "_journal_cut", lambda _name: None)
    with pytest.raises(export_mod.PublishError, match="journal"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()
