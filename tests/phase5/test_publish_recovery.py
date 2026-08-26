from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase5.test_result_export import CHANGE_ID, TARGET_A, TARGET_B, write_achieved

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
