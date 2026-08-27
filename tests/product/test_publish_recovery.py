from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.test_result_export import CHANGE_ID, TARGET_A, TARGET_B, write_achieved

_PHASES = ("prepared", "replacing", "committed")


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

    def boom(parent: Path, temporary_name: str, target_name: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            original(parent, temporary_name, target_name)
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
