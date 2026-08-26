from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.phase5.test_result_export import CHANGE_ID, TARGET_A, write_achieved


def test_publish_rejects_symlink_source(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    source = project / "qa" / "changes" / CHANGE_ID / "generated" / "api" / "files" / TARGET_A
    payload = source.read_bytes()
    outside = tmp_path / "outside-source"
    outside.write_bytes(payload)
    source.unlink()
    source.symlink_to(outside)
    original = (project / TARGET_A).read_bytes()

    with pytest.raises(PublishError, match="symlink"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == original


def test_publish_rejects_symlink_target(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    target = project / TARGET_A
    payload = target.read_bytes()
    outside = tmp_path / "outside-target"
    outside.write_bytes(payload)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(PublishError, match="symlink"):
        publish_achieved(project, CHANGE_ID)

    assert target.is_symlink()
    assert outside.read_bytes() == payload


def test_publish_rejects_hardlink_source(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    source = project / "qa" / "changes" / CHANGE_ID / "generated" / "api" / "files" / TARGET_A
    linked = tmp_path / "hardlink-source"
    os.link(source, linked)
    original = (project / TARGET_A).read_bytes()

    with pytest.raises(PublishError, match="hard ?link"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == original


def test_publish_rejects_hardlink_target(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    target = project / TARGET_A
    linked = tmp_path / "hardlink-target"
    os.link(target, linked)

    with pytest.raises(PublishError, match="hard ?link"):
        publish_achieved(project, CHANGE_ID)

    assert target.read_bytes() == b"original-a\n"


def test_publish_rejects_special_file_source(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    source = project / "qa" / "changes" / CHANGE_ID / "generated" / "api" / "files" / TARGET_A
    source.unlink()
    os.mkfifo(source)
    original = (project / TARGET_A).read_bytes()

    with pytest.raises(PublishError, match="regular|special"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == original


def test_publish_uses_adjacent_temp_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(tmp_path, files=((TARGET_A, b"generated-a\n", b"original-a\n"),))

    def crash(phase: str) -> None:
        if phase == "prepared":
            raise RuntimeError("forced prepared crash")

    monkeypatch.setattr(export_mod, "_journal_cut", crash)
    with pytest.raises(RuntimeError, match="prepared"):
        export_mod.publish_achieved(project, CHANGE_ID)

    journal = PublishJournalV1.model_validate_json(
        (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
    )
    record = journal.records[-1]
    assert record.phase == "prepared"
    assert record.files[0].target_path == TARGET_A
    temp = (project / TARGET_A).parent / record.files[0].temp_name
    assert temp.is_file()
    assert temp.parent == (project / TARGET_A).parent
    assert temp.read_bytes() == b"generated-a\n"
    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()

    monkeypatch.setattr(export_mod, "_journal_cut", lambda _phase: None)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert receipt.final_digest == receipt.source_digest
    assert not temp.exists()
