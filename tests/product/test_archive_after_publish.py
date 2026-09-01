from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.product.cli_support import parse_json_output
from tests.product.test_result_export import CHANGE_ID, TARGET_A, TARGET_B, UNLISTED, write_achieved


def _change_root(project: Path, change_id: str = CHANGE_ID) -> Path:
    return project / "qa" / "changes" / change_id


def _archive_root(project: Path, change_id: str = CHANGE_ID) -> Path:
    return project / "qa" / "archive" / change_id


def _tree_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _write_receipt(project: Path, payload: object, *, change_id: str = CHANGE_ID) -> Path:
    path = _change_root(project, change_id) / "publish-receipt.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _load_receipt(project: Path) -> dict[str, object]:
    payload = json.loads((_change_root(project) / "publish-receipt.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("publish receipt must be an object")
    return payload


def _receipt_files(receipt: dict[str, object]) -> list[dict[str, object]]:
    files = receipt.get("files")
    if not isinstance(files, list):
        raise TypeError("publish receipt files must be a list")
    return [dict(item) for item in files if isinstance(item, dict)]


def _flip_digest(value: str) -> str:
    return value[:-1] + ("0" if value[-1] != "0" else "1")


def _assert_archive_rejects_receipt(
    project: Path, *, published: bytes, change_files: dict[str, bytes]
) -> None:
    from assurance_product.status import ArchiveError, archive_published

    with pytest.raises(ArchiveError, match="receipt|manifest"):
        archive_published(project, CHANGE_ID)

    assert _change_root(project).is_dir()
    assert not _archive_root(project).exists()
    assert _tree_files(_change_root(project)) == change_files
    assert (project / TARGET_A).read_bytes() == published
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"


def test_archive_before_publish_fails_unchanged(tmp_path: Path) -> None:
    from assurance_product.status import ArchiveError, archive_published

    project = write_achieved(tmp_path)
    original = (project / TARGET_A).read_bytes()
    change_files = _tree_files(_change_root(project))

    with pytest.raises(ArchiveError, match="publish"):
        archive_published(project, CHANGE_ID)

    assert _change_root(project).is_dir()
    assert not _archive_root(project).exists()
    assert _tree_files(_change_root(project)) == change_files
    assert (project / TARGET_A).read_bytes() == original
    assert (project / TARGET_B).read_bytes() == b"original-b\n"


def test_archive_invalid_receipt_fails_unchanged(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import ArchiveError, archive_published

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    _write_receipt(project, {"schema_version": "1", "change_id": CHANGE_ID})
    published = (project / TARGET_A).read_bytes()
    change_files = _tree_files(_change_root(project))

    with pytest.raises(ArchiveError, match="receipt"):
        archive_published(project, CHANGE_ID)

    assert _change_root(project).is_dir()
    assert not _archive_root(project).exists()
    assert _tree_files(_change_root(project)) == change_files
    assert (project / TARGET_A).read_bytes() == published


def test_archive_preserves_complete_change_record(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import archive_published

    project = write_achieved(tmp_path)
    extra = _change_root(project) / "report" / "report.md"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"# archived report\n")
    publish_achieved(project, CHANGE_ID)
    before = _tree_files(_change_root(project))

    result = archive_published(project, CHANGE_ID)

    assert result["change_id"] == CHANGE_ID
    assert result["archive_root"] == f"qa/archive/{CHANGE_ID}"
    assert not _change_root(project).exists()
    assert _archive_root(project).is_dir()
    assert _tree_files(_archive_root(project)) == before
    assert (_archive_root(project) / "publish-receipt.json").is_file()
    assert (_archive_root(project) / "status.json").is_file()
    assert (_archive_root(project) / "apply-manifest.json").is_file()
    assert (_archive_root(project) / "report" / "report.md").read_bytes() == b"# archived report\n"


def test_archive_never_performs_publication(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import ArchiveError, archive_published

    unpublished = write_achieved(tmp_path)
    unpublished_target = (unpublished / TARGET_A).read_bytes()
    unpublished_extra = (unpublished / UNLISTED).read_bytes()

    with pytest.raises(ArchiveError):
        archive_published(unpublished, CHANGE_ID)

    assert (unpublished / TARGET_A).read_bytes() == unpublished_target
    assert (unpublished / UNLISTED).read_bytes() == unpublished_extra
    assert unpublished_target == b"original-a\n"

    published = write_achieved(tmp_path / "published")
    publish_achieved(published, CHANGE_ID)
    target_mtime = (published / TARGET_A).stat().st_mtime_ns
    extra_mtime = (published / UNLISTED).stat().st_mtime_ns

    archive_published(published, CHANGE_ID)

    assert (published / TARGET_A).read_bytes() == b"generated-a\n"
    assert (published / TARGET_A).stat().st_mtime_ns == target_mtime
    assert (published / UNLISTED).read_bytes() == b"keep-unlisted\n"
    assert (published / UNLISTED).stat().st_mtime_ns == extra_mtime


def test_cli_archive_requires_explicit_change(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)

    result = cli_runner.invoke(app, ["archive", "--project-dir", str(project)])

    assert result.exit_code != 0
    assert "--change" in result.output
    assert _change_root(project).is_dir()
    assert not _archive_root(project).exists()


def test_archive_copies_when_filesystems_differ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product import status as status_module

    project = write_achieved(tmp_path)
    extra = _change_root(project) / "inspect" / "inspection.json"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b'{"coverage":{"decision":true}}\n')
    publish_achieved(project, CHANGE_ID)
    before = _tree_files(_change_root(project))
    monkeypatch.setattr(status_module, "_same_filesystem", lambda _source, _dest: False)

    result = status_module.archive_published(project, CHANGE_ID)

    assert result["archive_root"] == f"qa/archive/{CHANGE_ID}"
    assert not _change_root(project).exists()
    assert _tree_files(_archive_root(project)) == before
    assert not list(_archive_root(project).parent.glob(".*.tmp"))
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"


def test_cli_archive_after_publish(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    before = _tree_files(_change_root(project))

    result = cli_runner.invoke(
        app,
        ["archive", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )

    assert result.exit_code == 0, result.output
    document = parse_json_output(result.stdout)
    assert document["change_id"] == CHANGE_ID
    assert document["archive_root"] == f"qa/archive/{CHANGE_ID}"
    assert not _change_root(project).exists()
    assert _tree_files(_archive_root(project)) == before
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"


def test_archive_rejects_wrong_manifest_digest(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = _load_receipt(project)
    _write_receipt(project, {**receipt, "manifest_digest": _flip_digest(str(receipt["manifest_digest"]))})
    _assert_archive_rejects_receipt(
        project,
        published=(project / TARGET_A).read_bytes(),
        change_files=_tree_files(_change_root(project)),
    )


def test_archive_rejects_extra_receipt_file(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = _load_receipt(project)
    files = _receipt_files(receipt)
    extra = dict(files[0])
    extra["target_path"] = "tests/api/extra.py"
    extra["temp_name"] = "extra.tmp"
    extra["backup_name"] = "extra.bak"
    receipt["files"] = [*files, extra]
    _write_receipt(project, receipt)
    _assert_archive_rejects_receipt(
        project,
        published=(project / TARGET_A).read_bytes(),
        change_files=_tree_files(_change_root(project)),
    )


def test_archive_rejects_missing_receipt_file(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = _load_receipt(project)
    receipt["files"] = _receipt_files(receipt)[1:]
    _write_receipt(project, receipt)
    _assert_archive_rejects_receipt(
        project,
        published=(project / TARGET_A).read_bytes(),
        change_files=_tree_files(_change_root(project)),
    )


def test_archive_rejects_tampered_source_digest(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = _load_receipt(project)
    _write_receipt(project, {**receipt, "source_digest": _flip_digest(str(receipt["source_digest"]))})
    _assert_archive_rejects_receipt(
        project,
        published=(project / TARGET_A).read_bytes(),
        change_files=_tree_files(_change_root(project)),
    )


def test_archive_rejects_tampered_final_digest(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = _load_receipt(project)
    _write_receipt(project, {**receipt, "final_digest": _flip_digest(str(receipt["final_digest"]))})
    _assert_archive_rejects_receipt(
        project,
        published=(project / TARGET_A).read_bytes(),
        change_files=_tree_files(_change_root(project)),
    )


def test_archive_rejects_mismatched_rename_leftover(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import ArchiveError, archive_published

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    source = _change_root(project)
    destination = _archive_root(project)
    shutil.copytree(source, destination)
    (destination / "status.json").write_bytes(b'{"tampered":true}\n')
    change_files = _tree_files(source)
    published = (project / TARGET_A).read_bytes()

    with pytest.raises(ArchiveError, match="archive destination already exists"):
        archive_published(project, CHANGE_ID)

    assert source.is_dir()
    assert destination.is_dir()
    assert _tree_files(source) == change_files
    assert (project / TARGET_A).read_bytes() == published


def test_archive_resumes_committed_rename_leftover_without_publishing(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import archive_published

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    source = _change_root(project)
    destination = _archive_root(project)
    shutil.copytree(source, destination)
    leftover = _tree_files(source)
    published = (project / TARGET_A).read_bytes()
    published_mtime = (project / TARGET_A).stat().st_mtime_ns

    result = archive_published(project, CHANGE_ID)

    assert result["change_id"] == CHANGE_ID
    assert result["archive_root"] == f"qa/archive/{CHANGE_ID}"
    assert not source.exists()
    assert _tree_files(destination) == leftover
    assert (project / TARGET_A).read_bytes() == published
    assert (project / TARGET_A).stat().st_mtime_ns == published_mtime


def test_archive_returns_existing_archive_when_change_is_gone(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.status import archive_published

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    first = archive_published(project, CHANGE_ID)
    archived = _tree_files(_archive_root(project))
    published = (project / TARGET_A).read_bytes()
    published_mtime = (project / TARGET_A).stat().st_mtime_ns

    result = archive_published(project, CHANGE_ID)

    assert result == first
    assert not _change_root(project).exists()
    assert _tree_files(_archive_root(project)) == archived
    assert (project / TARGET_A).read_bytes() == published
    assert (project / TARGET_A).stat().st_mtime_ns == published_mtime


def test_cli_archive_does_not_call_legacy_engine(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app
    from assurance_product.export import publish_achieved
    from graph_engine.runtime import driver, engine

    def _forbid(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("archive must not call legacy Engine or driver")

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    monkeypatch.setattr(engine, "Engine", _forbid)
    monkeypatch.setattr(driver, "acquire_invocation", _forbid)
    result = cli_runner.invoke(
        app,
        ["archive", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert result.exit_code == 0, result.output
