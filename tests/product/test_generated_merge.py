from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from typing import cast

from assurance_product.generated_merge import (
    GeneratedFileV2,
    GeneratedOperation,
    MergedGeneratedSet,
    TestFamily,
    merge_generated,
)

FAMILIES = ("api", "e2e", "fuzz", "performance")
CHANGE_ID = "CH-DEMO-001"
FAMILY_TARGETS = {
    "api": "tests/api/test_users.py",
    "e2e": "tests/e2e/test_users.py",
    "fuzz": "tests/fuzz/test_users.py",
    "performance": "tests/perf/test_users.py",
}
SHARED_TARGET = "tests/testdata/domain/users.py"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _staged_path(family: str, target: str, change_id: str = CHANGE_ID) -> str:
    return f"qa/changes/{change_id}/generated/{family}/files/{target}"


def _write_bytes(root: Path, relative: str, content: bytes, *, mode: int | None = None) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mode is not None:
        path.chmod(mode)
    return path


def _write_manifest(
    project: Path,
    family: str,
    files: list[Mapping[str, object]],
    *,
    change_id: str = CHANGE_ID,
) -> None:
    document = {
        "schema_version": "1",
        "change_id": change_id,
        "layer": family,
        "files": list(files),
        "mapping": {
            "schema_version": "1",
            "layer": family,
            "entries": [
                {
                    "case_id": f"TC_{family.upper()}_001",
                    "symbol": f"test_tc_{family}_001__happy_path",
                    "target_file": files[0]["target_path"],
                }
            ]
            if files
            else [],
        },
        "required_capabilities": [],
    }
    _write_bytes(
        project,
        f"qa/changes/{change_id}/codegen/{family}-generated-files.json",
        json.dumps(document).encode("utf-8"),
    )


def _promote_family(
    project: Path,
    family: str,
    target: str,
    content: bytes,
    *,
    operation: str = "generated",
    mode: int | None = None,
    change_id: str = CHANGE_ID,
) -> GeneratedFileV2:
    path = _write_bytes(project, _staged_path(family, target, change_id), content, mode=mode)
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    digest = _digest(content)
    _write_manifest(
        project,
        family,
        [
            {
                "target_path": target,
                "disposition": operation,
                "role": "test_entry",
                "case_ids": [f"TC_{family.upper()}_001"],
                "content_sha256": digest,
            }
        ],
        change_id=change_id,
    )
    return GeneratedFileV2(
        target_path=target,
        staged_path=_staged_path(family, target, change_id),
        sha256=digest,
        mode=actual_mode,
        operation=cast(GeneratedOperation, operation),
        family=cast(TestFamily, family),
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    return project


def test_merge_generated_uses_four_physical_family_namespaces(tmp_path: Path) -> None:
    project = _project(tmp_path)
    expected = [
        _promote_family(project, family, FAMILY_TARGETS[family], f"{family}\n".encode())
        for family in FAMILIES
    ]

    merged = merge_generated(project, CHANGE_ID, FAMILIES)

    assert isinstance(merged, MergedGeneratedSet)
    assert [item.family for item in merged.files] == list(FAMILIES)
    assert [item.target_path for item in merged.files] == [FAMILY_TARGETS[family] for family in FAMILIES]
    assert [item.staged_path for item in merged.files] == [
        _staged_path(family, FAMILY_TARGETS[family]) for family in FAMILIES
    ]
    assert merged.sources == {item.target_path: item.staged_path for item in expected}
    for item, promoted in zip(merged.files, expected, strict=True):
        assert item == promoted
    assert not (project / "tests" / "api" / "test_users.py").exists()


def test_merge_generated_rejects_a_manifest_member_missing_on_disk(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_manifest(
        project,
        "api",
        [
            {
                "target_path": FAMILY_TARGETS["api"],
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": ["TC_API_001"],
                "content_sha256": _digest(b"missing\n"),
            }
        ],
    )

    with pytest.raises(ValueError, match="closed manifest"):
        merge_generated(project, CHANGE_ID, ("api",))


def test_merge_generated_rejects_extra_on_disk_files(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote_family(project, "api", FAMILY_TARGETS["api"], b"api\n")
    _write_bytes(project, _staged_path("api", "tests/api/extra.py"), b"extra\n")

    with pytest.raises(ValueError, match="extra"):
        merge_generated(project, CHANGE_ID, ("api",))


def test_merge_generated_accepts_identical_duplicate_ownership(tmp_path: Path) -> None:
    project = _project(tmp_path)
    content = b"shared-builder\n"
    api = _promote_family(project, "api", SHARED_TARGET, content)
    e2e = _promote_family(project, "e2e", SHARED_TARGET, content)

    merged = merge_generated(project, CHANGE_ID, ("e2e", "api"))

    assert len(merged.files) == 1
    assert merged.files[0].target_path == SHARED_TARGET
    assert merged.files[0].sha256 == api.sha256 == e2e.sha256
    assert merged.files[0].mode == api.mode == e2e.mode
    assert merged.files[0].operation == "generated"
    assert merged.files[0].family == "api"
    assert merged.sources == {SHARED_TARGET: api.staged_path}


@pytest.mark.parametrize("field", ("bytes", "mode", "operation"))
def test_merge_generated_rejects_conflicting_shared_ownership(tmp_path: Path, field: str) -> None:
    project = _project(tmp_path)
    content = b"shared-builder\n"
    _promote_family(project, "api", SHARED_TARGET, content, operation="generated")
    if field == "bytes":
        _promote_family(project, "e2e", SHARED_TARGET, b"other-bytes\n", operation="generated")
    elif field == "mode":
        _promote_family(project, "e2e", SHARED_TARGET, content, operation="generated", mode=0o600)
    else:
        _promote_family(project, "e2e", SHARED_TARGET, content, operation="updated")

    with pytest.raises(ValueError, match="conflict"):
        merge_generated(project, CHANGE_ID, ("api", "e2e"))


@pytest.mark.parametrize(
    "target_path",
    ("../escape.py", "/tmp/escape.py", "tests/api/../../secret.py", "tests/api/bad\x00name.py"),
)
def test_merge_generated_rejects_path_traversal(tmp_path: Path, target_path: str) -> None:
    project = _project(tmp_path)
    _write_manifest(
        project,
        "api",
        [
            {
                "target_path": target_path,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": ["TC_API_001"],
                "content_sha256": _digest(b"escape\n"),
            }
        ],
    )

    with pytest.raises(ValueError, match="path"):
        merge_generated(project, CHANGE_ID, ("api",))


def test_merge_generated_order_is_independent_of_family_completion_order(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for family in FAMILIES:
        _promote_family(project, family, FAMILY_TARGETS[family], f"{family}\n".encode())

    first = merge_generated(project, CHANGE_ID, ("performance", "fuzz", "e2e", "api"))
    second = merge_generated(project, CHANGE_ID, FAMILIES)

    assert [item.target_path for item in first.files] == [item.target_path for item in second.files]
    assert first.digest == second.digest
    assert first.sources == second.sources
    assert list(first.sources) == sorted(first.sources)


def test_merge_generated_does_not_write_the_sut(tmp_path: Path) -> None:
    project = _project(tmp_path)
    original = project / "tests" / "api" / "test_users.py"
    original.write_bytes(b"original-sut\n")
    _promote_family(project, "api", FAMILY_TARGETS["api"], b"generated\n")

    merged = merge_generated(project, CHANGE_ID, ("api",))

    assert original.read_bytes() == b"original-sut\n"
    assert merged.sources[FAMILY_TARGETS["api"]].startswith(f"qa/changes/{CHANGE_ID}/generated/api/files/")
    assert (project / merged.sources[FAMILY_TARGETS["api"]]).read_bytes() == b"generated\n"
