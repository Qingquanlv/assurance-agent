from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from tests.product.test_result_export import CHANGE_ID, TARGET_A, write_achieved


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


@pytest.mark.parametrize("replacement_kind", ("symlink", "hardlink", "regular"))
def test_destination_swap_after_authentication_before_replace_fails_closed_without_clobber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    from assurance_product import export as export_mod

    project = write_achieved(tmp_path)
    target = project / TARGET_A
    victim = tmp_path / f"victim-{replacement_kind}"
    victim.write_bytes(b"victim must remain unchanged\n")
    swapped: list[str] = []

    def swap_at(phase: str) -> None:
        if phase != "replacing" or swapped:
            return
        swapped.append(phase)
        target.unlink()
        if replacement_kind == "symlink":
            target.symlink_to(victim)
        elif replacement_kind == "hardlink":
            os.link(victim, target)
        else:
            target.write_bytes(victim.read_bytes())

    monkeypatch.setattr(export_mod, "_journal_cut", swap_at)
    with pytest.raises(export_mod.PublishError, match="rolled back|baseline|changed"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert swapped == ["replacing"]
    assert victim.read_bytes() == b"victim must remain unchanged\n"
    assert target.read_bytes() == b"victim must remain unchanged\n"
    if replacement_kind == "symlink":
        assert target.is_symlink()
    elif replacement_kind == "hardlink":
        assert target.stat().st_ino == victim.stat().st_ino
        assert target.stat().st_nlink == 2
    else:
        assert not target.is_symlink()
        assert target.stat().st_ino != victim.stat().st_ino
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_publish_rejects_temporary_symlink_swapped_after_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod
    from assurance_product.models import PublishJournalV1

    project = write_achieved(tmp_path)
    victim = tmp_path / "temporary-symlink-victim"
    victim.write_bytes(b"victim must remain unchanged\n")
    swapped: list[Path] = []

    def swap_prepared_temporary(phase: str) -> None:
        if phase != "replacing" or swapped:
            return
        journal = PublishJournalV1.model_validate_json(
            (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").read_bytes()
        )
        published_file = journal.records[-1].files[0]
        temporary = (project / published_file.target_path).parent / published_file.temp_name
        temporary.unlink()
        temporary.symlink_to(victim)
        swapped.append(temporary)

    monkeypatch.setattr(export_mod, "_journal_cut", swap_prepared_temporary)

    with pytest.raises(export_mod.PublishError, match="temporary|symlink|rolled back"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert len(swapped) == 1
    assert victim.read_bytes() == b"victim must remain unchanged\n"
    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_publish_rejects_intermediate_parent_symlink_swap_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod

    target = "nested/deeper/test_generated.py"
    project = write_achieved(
        tmp_path,
        files=((target, b"generated\n", b"baseline\n"),),
    )
    original_ancestor = project / "nested"
    moved_ancestor = project / "nested-authenticated"
    outside_ancestor = tmp_path / "outside"
    outside_target = outside_ancestor / "deeper" / "test_generated.py"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_bytes(b"outside must remain unchanged\n")
    swapped: list[str] = []

    def swap_intermediate_ancestor(cut: str) -> None:
        if cut != "export-before-replace-authentication" or swapped:
            return
        swapped.append(cut)
        original_ancestor.rename(moved_ancestor)
        original_ancestor.symlink_to(outside_ancestor, target_is_directory=True)

    monkeypatch.setattr(export_mod, "_publication_cut", swap_intermediate_ancestor)

    with pytest.raises(
        export_mod.PublishError,
        match="parent|ancestor|symlink|rolled back|path escapes",
    ):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert swapped == ["export-before-replace-authentication"]
    assert outside_target.read_bytes() == b"outside must remain unchanged\n"
    assert (moved_ancestor / "deeper" / "test_generated.py").read_bytes() == b"baseline\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_publish_fails_closed_while_change_publication_lock_is_held(tmp_path: Path) -> None:
    from assurance_product import export as export_mod

    project = write_achieved(tmp_path)
    change_root = project / "qa" / "changes" / CHANGE_ID
    lock_path = change_root / ".publish.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(export_mod.PublishError, match="already in progress|lock"):
            export_mod.publish_achieved(project, CHANGE_ID)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert (project / TARGET_A).read_bytes() == b"original-a\n"
    assert not (change_root / "publish-receipt.json").exists()


def test_already_matching_target_rejects_intermediate_parent_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import export as export_mod

    target = "nested/deeper/already_generated.py"
    project = write_achieved(
        tmp_path,
        files=((target, b"generated\n", b"generated\n"),),
    )
    original_ancestor = project / "nested"
    moved_ancestor = project / "nested-authenticated"
    swapped: list[str] = []

    def swap_matching_parent(phase: str) -> None:
        if phase != "replacing" or swapped:
            return
        swapped.append(phase)
        original_ancestor.rename(moved_ancestor)
        original_ancestor.symlink_to(moved_ancestor, target_is_directory=True)

    monkeypatch.setattr(export_mod, "_journal_cut", swap_matching_parent)

    with pytest.raises(export_mod.PublishError, match="parent|ancestor|symlink|rolled back"):
        export_mod.publish_achieved(project, CHANGE_ID)

    assert swapped == ["replacing"]
    assert (moved_ancestor / "deeper" / "already_generated.py").read_bytes() == b"generated\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()
