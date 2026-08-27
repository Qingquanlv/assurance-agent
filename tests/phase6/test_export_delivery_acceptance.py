from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.test_result_export import (
    CHANGE_ID,
    README,
    TARGET_A,
    TARGET_B,
    UNLISTED,
    write_achieved,
)


def _change_root(project: Path, change_id: str = CHANGE_ID) -> Path:
    return project / "qa" / "changes" / change_id


def _archive_root(project: Path, change_id: str = CHANGE_ID) -> Path:
    return project / "qa" / "archive" / change_id


def test_export_rejects_non_achieved_change(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path, state="running", publication="not_ready")
    original = (project / TARGET_A).read_bytes()

    with pytest.raises(PublishError, match="achieved"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == original
    assert not (_change_root(project) / "publish-receipt.json").exists()


def test_export_rejects_target_baseline_drift(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    (project / TARGET_A).write_bytes(b"drifted-target\n")

    with pytest.raises(PublishError, match="drift|baseline"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"drifted-target\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    assert not (_change_root(project) / "publish-receipt.json").exists()


def test_export_writes_authenticated_manifest_file_set(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.models import PublishReceiptV1

    files = (
        (TARGET_A, b"generated-a\n", b"original-a\n"),
        (TARGET_B, b"generated-b\n", b"original-b\n"),
    )
    project = write_achieved(tmp_path, files=files)
    extra = project / UNLISTED
    readme = project / README
    extra_mtime = extra.stat().st_mtime_ns
    readme_bytes = readme.read_bytes()

    receipt = publish_achieved(project, CHANGE_ID)

    assert isinstance(receipt, PublishReceiptV1)
    assert receipt.change_id == CHANGE_ID
    assert {item.target_path for item in receipt.files} == {TARGET_A, TARGET_B}
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"
    assert extra.read_bytes() == b"keep-unlisted\n"
    assert extra.stat().st_mtime_ns == extra_mtime
    assert readme.read_bytes() == readme_bytes
    written = PublishReceiptV1.model_validate_json(
        (_change_root(project) / "publish-receipt.json").read_bytes()
    )
    assert written == receipt
    assert not any(project.rglob("result-tree"))
    assert not any(project.rglob("HEAD.json"))


def test_export_recovers_after_interrupted_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        (_change_root(project) / "publish-journal.json").read_bytes()
    )
    assert journal.records[-1].phase == "prepared"
    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert not (_change_root(project) / "publish-receipt.json").exists()

    monkeypatch.setattr(export_mod, "_journal_cut", lambda _phase: None)
    receipt = export_mod.publish_achieved(project, CHANGE_ID)

    assert receipt.change_id == CHANGE_ID
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (_change_root(project) / "publish-receipt.json").is_file()


def test_export_is_idempotent(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    first = publish_achieved(project, CHANGE_ID)
    second = publish_achieved(project, CHANGE_ID)

    assert second == first
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"


def test_archive_after_publish_receipt(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import archive_published

    project = write_achieved(tmp_path)
    extra = _change_root(project) / "report" / "report.md"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"# archived report\n")
    receipt = publish_achieved(project, CHANGE_ID)
    change_files = {
        path.relative_to(_change_root(project)).as_posix(): path.read_bytes()
        for path in sorted(_change_root(project).rglob("*"))
        if path.is_file() and not path.is_symlink()
    }

    result = archive_published(project, CHANGE_ID)

    assert result["change_id"] == CHANGE_ID
    assert result["archive_root"] == f"qa/archive/{CHANGE_ID}"
    assert not _change_root(project).exists()
    assert _archive_root(project).is_dir()
    archived = {
        path.relative_to(_archive_root(project)).as_posix(): path.read_bytes()
        for path in sorted(_archive_root(project).rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    assert archived == change_files
    assert (_archive_root(project) / "publish-receipt.json").read_bytes()
    assert receipt.change_id == CHANGE_ID
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / UNLISTED).read_bytes() == b"keep-unlisted\n"
